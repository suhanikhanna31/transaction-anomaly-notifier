# Transaction Anomaly Detection API

A backend service that detects anomalous financial transactions using statistical thresholding and exposes predictions through a REST API.

## Tech Stack
- Python
- FastAPI
- Pandas
- NumPy
- Uvicorn

## Project Overview
This project simulates a real-world transaction monitoring system. It analyzes transaction amounts using statistical measures and flags anomalous transactions based on z-score thresholds. The detection sensitivity is tuned by adjusting the threshold value, similar to how production risk systems balance false positives and false negatives.

## How It Works
1. Loads historical transaction data from a CSV file
2. Computes mean and standard deviation of transaction amounts
3. Calculates z-score for incoming transactions
4. Flags transactions as ANOMALY or NORMAL based on threshold logic
5. Returns results as structured JSON responses

## API Endpoints

### GET /
Health check endpoint.

### POST /predict
Predict whether a transaction is anomalous.

**Query Parameter**
- `amount` (float): Transaction amount

**Example Request**
POST /predict?amount=5000

**Example Response**
{
  "amount": 5000,
  "status": "ANOMALY",
  "z_score": 2.31
}

## Running Locally
pip install -r requirements.txt
uvicorn main:app --reload

Open the API documentation at:
http://127.0.0.1:8000/docs

## Use Cases
- Fraud detection
- Transaction monitoring
- Risk scoring systems
- Backend services for ML-driven decision making

## Notes
This project focuses on backend system design and statistical anomaly detection rather than complex machine learning models. It can be extended with ML models, databases, and real-time data pipelines.