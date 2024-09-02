# main.py
import os
import subprocess
import requests
import yaml
import time
import smtplib
from email.message import EmailMessage
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
import structlog
import signal
import asyncio
from circuitbreaker import CircuitBreakerError, CircuitBreaker
from prometheus_client import Counter, Gauge, start_http_server
from tenacity import retry, stop_after_attempt, wait_fixed

# Structured logging configuration
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer()
    ]
)
logger = structlog.get_logger()

# Environment Variables for Configuration
MAIN_APP_PORT = int(os.getenv('MAIN_APP_PORT', 8000))
SUBAPP_PORTS = {
    'main': MAIN_APP_PORT,
    'subapp1': int(os.getenv('SUBAPP1_PORT', 8001)),
    'subapp2': int(os.getenv('SUBAPP2_PORT', 8002))
}
DOCKER_COMPOSE_FILE = os.getenv('DOCKER_COMPOSE_FILE', 'docker-compose.yml')
SCALING_FACTOR = int(os.getenv('SCALING_FACTOR', 1))  # Scaling factor for Docker Compose
HEALTH_CHECK_INTERVAL = int(os.getenv('HEALTH_CHECK_INTERVAL', 60))  # in seconds
ALERT_EMAIL = os.getenv('ALERT_EMAIL', 'admin@example.com')
SMTP_SERVER = os.getenv('SMTP_SERVER', 'smtp.example.com')
SMTP_PORT = int(os.getenv('SMTP_PORT', 587))
SMTP_USERNAME = os.getenv('SMTP_USERNAME', 'username')
SMTP_PASSWORD = os.getenv('SMTP_PASSWORD', 'password')

DOCKERFILE_TEMPLATE = """
FROM python:3.10-slim

WORKDIR /app

COPY . /app

RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8000

CMD ["gunicorn", "-w", "4", "-k", "uvicorn.workers.UvicornWorker", "{module_name}:app", "--bind", "0.0.0.0:8000"]
"""

# Prometheus metrics
requests_count = Counter('requests_count', 'Total number of requests')
requests_latency = Gauge('requests_latency', 'Average latency of requests')

class CustomCircuitBreaker(CircuitBreaker):
    def __init__(self, name, failure_threshold=3, recovery_timeout=60):
        super().__init__(failure_threshold=failure_threshold, recovery_timeout=recovery_timeout)
        self.name = name

    def call(self, func, *args, **kwargs):
        try:
            return super().call(func, *args, **kwargs)
        except CircuitBreakerError as e:
            logger.error(f"Circuit breaker triggered for {self.name}. Service temporarily unavailable.", exc_info=True)
            self.send_alert(f"Circuit breaker triggered for {self.name}. Service temporarily unavailable.")
            raise e

    def send_alert(self, message):
        try:
            msg = EmailMessage()
            msg.set_content(message)
            msg['Subject'] = f"Alert: Service Issue in {self.name}"
            msg['From'] = SMTP_USERNAME
            msg['To'] = ALERT_EMAIL

            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
                server.starttls()
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.send_message(msg)

            logger.info(f"Alert sent for {self.name} issue.")
        except Exception as e:
            logger.error(f"Failed to send alert email: {e}", exc_info=True)

class DynamicSubAppMiddleware(BaseHTTPMiddleware):
    async def __ainit__(self):
        await super().__ainit__()
        self.lock = asyncio.Lock()

        # Generate Docker Compose file
        self.generate_docker_compose()

        # Build Docker images
        self.build_docker_images()

        # Start Docker Compose services
        self.start_services()

        # Start health check task
        self.start_health_check_task()

        # Start Prometheus server
        start_http_server(8001)

        # Register signal handler for graceful shutdown
        signal.signal(signal.SIGTERM, self.graceful_shutdown)

    async def dispatch(self, request: Request, call_next):
        path = request.url.path.strip('/')
        subapp_name = 'main' if path == '' else path.split('/')[0]

        if subapp_name in SUBAPP_PORTS:
            request.scope['root_path'] = f'/{subapp_name}'
            try:
                url = f"http://localhost:{SUBAPP_PORTS[subapp_name]}{request.url.path}"
                response = requests.get(url)
                response.raise_for_status()
                return response
            except requests.RequestException as e:
                logger.error(f"Error processing request for {subapp_name}", exc_info=True)
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Subapp {subapp_name} is currently unavailable: {str(e)}")

        return await call_next(request)

    def generate_docker_compose(self):
        services = {}
        for subapp_name, port in SUBAPP_PORTS.items():
            services[subapp_name] = {
                'build': f'./subapps/{subapp_name}' if subapp_name != 'main' else '.',
                'ports': [f"{port}:8000"],
                'deploy': {
                    'replicas': SCALING_FACTOR,
                    'restart_policy': {
                        'condition': 'on-failure',
                        'delay': '5s',
                        'max_attempts': 3,
                        'window': '120s'
                    }
                },
                'healthcheck': {
                    'test': ['CMD-SHELL', f'curl --fail http://localhost:{port}/health || exit 1'],
                    'interval': '30s',
                    'timeout': '10s',
                    'retries': 3,
                    'start_period': '10s'
                }
            }

        compose_dict = {
            'version': '3.8',
            'services': services
        }

        # Write to docker-compose.yml
        with open(DOCKER_COMPOSE_FILE, 'w') as file:
            yaml.dump(compose_dict, file, default_flow_style=False)
        logger.info("Generated docker-compose.yml successfully.")

    def build_docker_images(self):
        try:
            for subapp_name in SUBAPP_PORTS.keys():
                # Create Dockerfile content dynamically
                dockerfile_content = DOCKERFILE_TEMPLATE.format(module_name=subapp_name)
                
                # Save Dockerfile
                dockerfile_path = f"./subapps/{subapp_name}/Dockerfile" if subapp_name != 'main' else './Dockerfile'
                os.makedirs(os.path.dirname(dockerfile_path), exist_ok=True)
                with open(dockerfile_path, "w") as f:
                    f.write(dockerfile_content)

                # Build Docker image
                subprocess.run(["docker-compose", "build", subapp_name], check=True)
                logger.info(f"Built Docker image for {subapp_name} successfully.")
        except subprocess.CalledProcessError as e:
            logger.error("Failed to build Docker images.", exc_info=True)
            raise

    def start_services(self):
        try:
            subprocess.run(["docker-compose", "-f", DOCKER_COMPOSE_FILE, "up", "-d"], check=True)
            logger.info("Docker Compose services started successfully.")
        except subprocess.CalledProcessError as e:
            logger.error("Failed to start Docker Compose services.", exc_info=True)
            raise

    def stop_services(self):
        try:
            subprocess.run(["docker-compose", "-f", DOCKER_COMPOSE_FILE, "down"], check=True)
            logger.info("Docker Compose services stopped successfully.")
        except subprocess.CalledProcessError as e:
            logger.error("Failed to stop Docker Compose services.", exc_info=True)
            raise

    def restart_service(self, subapp_name):
        try:
            subprocess.run(["docker-compose", "restart", subapp_name], check=True)
            logger.info(f"Restarted service: {subapp_name}")
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to restart service {subapp_name}.", exc_info=True)
            raise

    def scale_service(self, subapp_name, scale_factor):
        try:
            subprocess.run(
                ["docker-compose", "-f", DOCKER_COMPOSE_FILE, "up", "--scale", f"{subapp_name}={scale_factor}", "-d"],
                check=True
            )
            logger.info(f"Scaled {subapp_name} to {scale_factor} replicas")
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to scale service {subapp_name}.", exc_info=True)
            raise

    async def health_check(self):
        while True:
            try:
                for subapp_name, port in SUBAPP_PORTS.items():
                    url = f"http://localhost:{port}/health"
                    response = requests.get(url)
                    if response.status_code != 200:
                        logger.error(f"Health check failed for {subapp_name}. Status code: {response.status_code}")
            except requests.RequestException as e:
                logger.error(f"Error during health check: {e}", exc_info=True)
                self.restart_service(subapp_name)
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)

    def start_health_check_task(self):
        loop = asyncio.get_event_loop()
        loop.create_task(self.health_check())

    def graceful_shutdown(self, signum, frame):
        logger.info("Received signal to shut down. Stopping services...")
        self.stop_services()
        logger.info("Services stopped. Exiting...")
        sys.exit(0)

app = FastAPI(middleware=[Middleware(DynamicSubAppMiddleware)])
