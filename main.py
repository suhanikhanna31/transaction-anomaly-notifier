from fastapi import FastAPI
import pandas as pd
import numpy as np

app = FastAPI()

df = pd.read_csv("transactions.csv")

mean_amount = df["amount"].mean()
std_amount = df["amount"].std()

@app.get("/")
def root():
    return {"message": "Transaction Anomaly Notifier Running"}

@app.post("/predict")
def predict(amount: float):
    z_score = (amount - mean_amount) / std_amount

    if abs(z_score) > 1.5:
        return {
            "amount": amount,
            "status": "ANOMALY",
            "z_score": round(z_score, 2)
        }
    else:
        return {
            "amount": amount,
            "status": "NORMAL",
            "z_score": round(z_score, 2)
        }