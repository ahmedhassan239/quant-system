import os
import pandas as pd
import glob
import joblib
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report

def train_model():
    print("--- Starting Machine Learning Pipeline ---", flush=True)
    print("Locating engineered feature datasets...", flush=True)
    
    file_pattern = "data/processed/*_features.csv"
    csv_files = glob.glob(file_pattern)
    
    if not csv_files:
        print("No *_features.csv files found. Run the feature engineering script first.", flush=True)
        return
        
    print(f"Found {len(csv_files)} datasets. Combining datasets...", flush=True)
    
    dfs = []
    for file in csv_files:
        df = pd.read_csv(file)
        dfs.append(df)
        
    # Combine all DataFrames into one large DataFrame
    master_df = pd.concat(dfs, ignore_index=True)
    print(f"Total combined rows: {len(master_df):,}", flush=True)
    
    # Drop rows with NaN values to be absolutely safe
    master_df = master_df.dropna()
    
    # Define the Target (y): Signal = 1 if Target_1h_Return > 0.015, else 0
    print("Defining Target variable (Signal: 1 if > 1.5% return, else 0)...", flush=True)
    master_df['Signal'] = (master_df['Target_1h_Return'] > 0.015).astype(int)
    
    # Define Features (X): Select only the numerical feature columns
    print("Selecting features and splitting data (shuffle=False to respect time-series)...", flush=True)
    feature_cols = [
        'open', 'high', 'low', 'close', 'volume',
        'RSI_14', 'MACD', 'MACD_signal', 'BB_high', 'BB_low',
        'ATR_14', 'SMA_50', 'SMA_200'
    ]
    
    # Ensure only these columns are used and non-numeric/future-looking columns are dropped
    X = master_df[feature_cols]
    y = master_df['Signal']
    
    # Split the data into training (80%) and testing (20%) sets
    # CRUCIAL: shuffle=False to respect time-series chronological order and prevent data leakage
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.20, shuffle=False)
    
    print(f"Training set: {len(X_train):,} rows", flush=True)
    print(f"Testing set: {len(X_test):,} rows", flush=True)
    
    # Initialize and train the model
    print("Training Random Forest Classifier (this may take a few minutes)...", flush=True)
    model = RandomForestClassifier(
        n_estimators=100, 
        max_depth=10, 
        random_state=42, 
        n_jobs=-1,
        class_weight='balanced'
    )
    
    model.fit(X_train, y_train)
    
    # Evaluate the model on the test set
    print("Evaluating model on the test set...", flush=True)
    y_pred = model.predict(X_test)
    
    # Print the classification report to the console
    print("\n" + "="*53, flush=True)
    print("                CLASSIFICATION REPORT", flush=True)
    print("="*53, flush=True)
    print(classification_report(y_test, y_pred, digits=4), flush=True)
    print("="*53 + "\n", flush=True)
    
    # Save the trained model to disk
    os.makedirs('models', exist_ok=True)
    model_filename = 'models/quant_rf_model.pkl'
    joblib.dump(model, model_filename)
    print(f"Model saved successfully to {model_filename}.", flush=True)
    print("--- Machine Learning Pipeline Completed! ---", flush=True)

if __name__ == "__main__":
    train_model()
