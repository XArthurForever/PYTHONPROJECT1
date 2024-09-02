from fastapi import FastAPI
app = FastAPI()


@app.get("/")
def read_root():
    return {"message": "Hello SubApp1"}


@app.get("/health")
def health_check():
    return {"status": "SubApp1 is Healthy"}


@app.get("/liveness")
def liveness_check():
    return {"status": "SubApp1 is liveness okay"}


@app.get("/readiness")
def readiness_check():
    return {"status": "SubApp1 is readiness"}
