from fastapi import FastAPI
app = FastAPI()

@app.get("/")
def read_root():
    return {"message": "Hello SubApp2"}

@app.get("/healthy")
def health_check():
    return {"status": "SubApp2 is Healthy"}

@app.get("/liveness")
def liveness_check():
    return {"status": "SubApp2 is liveness Okay fine thank you"}

@app.get("/readiness")
def readiness_check():
    return {"status": "SubApp2 is readiness"}
