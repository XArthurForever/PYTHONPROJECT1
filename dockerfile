# Stage 1: Build Stage
FROM python:3.11-slim AS builder

# Set the working directory
WORKDIR /main

# Copy the requirements file
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY main.py/ .

# Stage 2: Runtime Stage
FROM python:3.11-slim

# Set the working directory
WORKDIR /app

# Copy dependencies and application code from the builder stage
COPY --from=builder /main /main

# Install gunicorn and uvicorn (runtime dependencies)
RUN pip install --no-cache-dir gunicorn uvicorn[standard]

# Expose the port that the application will run on
EXPOSE 8000

# Set environment variables for Gunicorn
ENV MODULE_NAME=main
ENV APP_NAME=app

# Command to run the application with Gunicorn
CMD ["gunicorn", "-w", "4", "-k", "uvicorn.workers.UvicornWorker", "${MODULE_NAME}:${APP_NAME}", "--bind", "0.0.0.0:8000"]