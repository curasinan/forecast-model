"""Scikit-Learn: Zero to Hero, Part 1: Core Concepts and The Universal API Pattern."""

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from sklearn.model_selection import train_test_split

print("=" * 70)
print("SCIKIT-LEARN: ZERO TO HERO")
print("Part 1: Core Concepts and The Universal API Pattern")
print("=" * 70)


# THE GOLDEN RULE: Scikit-Learn's Consistent API

print("\n" + "=" * 70)
print("THE GOLDEN RULE: CONSISTENT API")
print("=" * 70)

api_pattern = """
Every scikit-learn model follows the SAME pattern:

┌─────────────────────────────────────────────────────────────────────────┐
│                     THE SCIKIT-LEARN API PATTERN                        │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│   1. IMPORT           from sklearn.xxx import ModelName                 │
│                                                                         │
│   2. CREATE           model = ModelName(hyperparameters)                │
│                                                                         │
│   3. FIT (train)      model.fit(X_train, y_train)                       │
│                       ↳ This runs gradient descent (or equivalent)     │
│                       ↳ This finds the optimal θ parameters             │
│                                                                         │
│   4. PREDICT          predictions = model.predict(X_test)               │
│                       ↳ This computes h(x) = θᵀx (or equivalent)       │
│                                                                         │
│   5. EVALUATE         score = model.score(X_test, y_test)               │
│                       ↳ Returns accuracy (classification) or R² (regression)│
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘

This pattern works for EVERY algorithm:
- Linear Regression
- Logistic Regression
- Decision Trees
- Random Forests
- Support Vector Machines
- K-Nearest Neighbors
- And many more...

Once you learn this pattern, you know how to use ANY scikit-learn model.
"""

print(api_pattern)


# EXAMPLE 1: Linear Regression (You Built This From Scratch!)

print("\n" + "=" * 70)
print("EXAMPLE 1: LINEAR REGRESSION")
print("=" * 70)

# Generate data (same house price example)
np.random.seed(42)
X = np.random.rand(100, 1) * 10  # House size (scaled)
y = 2.5 * X.squeeze() + 10 + np.random.randn(100) * 2  # Price with noise

# What you did manually:
print("""
What YOU implemented from scratch:
----------------------------------
1. Initialize θ = [0, 0]
2. Loop for many iterations:
   - predictions = X @ theta
   - errors = predictions - y
   - gradients = X.T @ errors / m
   - theta = theta - alpha * gradients
3. Return final theta
""")

# What scikit-learn does:
print("What scikit-learn does (same thing, one line):")
print("-" * 50)

from sklearn.linear_model import LinearRegression

# Step 1: Create the model
model = LinearRegression()

# Step 2: Fit (this runs the optimization internally)
model.fit(X, y)

# Step 3: Access the learned parameters
print(f"model.coef_ (θ₁):      {model.coef_[0]:.4f}")  # This is θ₁
print(f"model.intercept_ (θ₀): {model.intercept_:.4f}")  # This is θ₀
print(f"\nTrue values: θ₁ = 2.5, θ₀ = 10")

# Step 4: Make predictions
X_new = np.array([[5], [7], [9]])
predictions = model.predict(X_new)
print(f"\nPredictions for sizes 5, 7, 9:")
for x, pred in zip(X_new.flatten(), predictions):
    print(f"  Size {x} → Price ${pred:.2f}")

# Step 5: Evaluate (R² score)
score = model.score(X, y)
print(f"\nR² Score: {score:.4f}")
print("(1.0 = perfect fit, 0.0 = predicts mean only)")


# EXAMPLE 2: Logistic Regression (Classification)

print("\n" + "=" * 70)
print("EXAMPLE 2: LOGISTIC REGRESSION")
print("=" * 70)

# Generate classification data
from sklearn.datasets import make_classification

X, y = make_classification(
    n_samples=200,
    n_features=2,
    n_redundant=0,
    n_informative=2,
    n_clusters_per_class=1,
    random_state=42
)

print(f"Data shape: X={X.shape}, y={y.shape}")
print(f"Classes: {np.unique(y)}")

# Same pattern!
from sklearn.linear_model import LogisticRegression

# Create
model = LogisticRegression()

# Fit
model.fit(X, y)

# Predict
predictions = model.predict(X[:5])
print(f"\nFirst 5 predictions: {predictions}")
print(f"First 5 actual:      {y[:5]}")

# Predict probabilities (this is h(x) = sigmoid(θᵀx))
probabilities = model.predict_proba(X[:5])
print(f"\nPrediction probabilities (first 5):")
print(f"  P(class=0)  P(class=1)")
for i, prob in enumerate(probabilities):
    print(f"  {prob[0]:.4f}      {prob[1]:.4f}  → predicts class {predictions[i]}")

# Score (accuracy for classification)
accuracy = model.score(X, y)
print(f"\nAccuracy: {accuracy:.4f} ({accuracy*100:.1f}%)")

# Access parameters
print(f"\nLearned parameters:")
print(f"  θ (coef_):      {model.coef_}")
print(f"  θ₀ (intercept_): {model.intercept_}")


# THE CRITICAL CONCEPT: Train/Test Split

print("\n" + "=" * 70)
print("THE CRITICAL CONCEPT: TRAIN/TEST SPLIT")
print("=" * 70)

explanation = """
PROBLEM: If you train AND evaluate on the same data, you don't know
         if your model actually learned or just memorized.

SOLUTION: Split your data into two parts:
          - Training set (70-80%): Used to fit the model
          - Test set (20-30%): Used to evaluate performance

         The model NEVER sees the test set during training!

┌─────────────────────────────────────────────────────────────────────────┐
│                           YOUR DATA                                     │
│  ┌──────────────────────────────────┬─────────────────────┐             │
│  │         TRAINING SET             │      TEST SET       │             │
│  │            (80%)                 │       (20%)         │             │
│  │                                  │                     │             │
│  │   model.fit(X_train, y_train)    │   model.score(X_test, y_test)    │
│  │                                  │   model.predict(X_test)          │
│  └──────────────────────────────────┴─────────────────────┘             │
└─────────────────────────────────────────────────────────────────────────┘
"""
print(explanation)

# How to do it
from sklearn.model_selection import train_test_split

# Split the data
X_train, X_test, y_train, y_test = train_test_split(
    X, y,
    test_size=0.2,      # 20% for testing
    random_state=42     # For reproducibility
)

print(f"Original data: {X.shape[0]} samples")
print(f"Training set:  {X_train.shape[0]} samples ({X_train.shape[0]/X.shape[0]*100:.0f}%)")
print(f"Test set:      {X_test.shape[0]} samples ({X_test.shape[0]/X.shape[0]*100:.0f}%)")

# Proper workflow
model = LogisticRegression()
model.fit(X_train, y_train)         # Train on training data only!

train_accuracy = model.score(X_train, y_train)
test_accuracy = model.score(X_test, y_test)

print(f"\nTraining accuracy: {train_accuracy:.4f}")
print(f"Test accuracy:     {test_accuracy:.4f}")

print("""
If training accuracy >> test accuracy: OVERFITTING
   (Model memorized training data, doesn't generalize)
   
If both are low: UNDERFITTING
   (Model is too simple for the data)
   
If both are similar and high: GOOD FIT
""")


# FEATURE SCALING (Preprocessing)

print("\n" + "=" * 70)
print("FEATURE SCALING (PREPROCESSING)")
print("=" * 70)

print("""
Remember when you implemented feature normalization by hand?

X_normalized = (X - mean) / std

Scikit-learn has tools for this!
""")

from sklearn.preprocessing import StandardScaler, MinMaxScaler

# Create data with different scales
data = np.array([
    [1000, 2],     # Feature 1: thousands, Feature 2: single digits
    [2000, 3],
    [1500, 2.5],
    [3000, 4],
])

print("Original data:")
print(data)

# StandardScaler: mean=0, std=1 (what you implemented)
scaler = StandardScaler()
data_scaled = scaler.fit_transform(data)

print("\nAfter StandardScaler (mean=0, std=1):")
print(data_scaled)
print(f"Mean of each feature: {data_scaled.mean(axis=0)}")
print(f"Std of each feature:  {data_scaled.std(axis=0)}")

# MinMaxScaler: scales to [0, 1] range
minmax = MinMaxScaler()
data_minmax = minmax.fit_transform(data)

print("\nAfter MinMaxScaler (range [0, 1]):")
print(data_minmax)

print("""
IMPORTANT: Fit the scaler on TRAINING data only, then transform both!

    scaler.fit(X_train)           # Learn mean/std from training
    X_train_scaled = scaler.transform(X_train)
    X_test_scaled = scaler.transform(X_test)  # Use SAME mean/std

Or use fit_transform for training data:
    X_train_scaled = scaler.fit_transform(X_train)
    X_test_scaled = scaler.transform(X_test)
""")


# COMPLETE WORKFLOW EXAMPLE

print("\n" + "=" * 70)
print("COMPLETE WORKFLOW EXAMPLE")
print("=" * 70)

# 1. Load data
from sklearn.datasets import load_breast_cancer
data = load_breast_cancer()
X, y = data.data, data.target

print(f"Dataset: Breast Cancer Wisconsin")
print(f"Features: {X.shape[1]}")
print(f"Samples: {X.shape[0]}")
print(f"Classes: {data.target_names}")

# 2. Split data
X_train, X_test, y_train, y_test = train_test_split(
    X, y, test_size=0.2, random_state=42
)

# 3. Scale features
scaler = StandardScaler()
X_train_scaled = scaler.fit_transform(X_train)
X_test_scaled = scaler.transform(X_test)

# 4. Create and train model
model = LogisticRegression(max_iter=1000)
model.fit(X_train_scaled, y_train)

# 5. Evaluate
train_acc = model.score(X_train_scaled, y_train)
test_acc = model.score(X_test_scaled, y_test)

print(f"\nResults:")
print(f"  Training Accuracy: {train_acc:.4f} ({train_acc*100:.1f}%)")
print(f"  Test Accuracy:     {test_acc:.4f} ({test_acc*100:.1f}%)")

# 6. Make predictions on new data
sample = X_test_scaled[0:1]
prediction = model.predict(sample)
probability = model.predict_proba(sample)

print(f"\nSample prediction:")
print(f"  Predicted class: {data.target_names[prediction[0]]}")
print(f"  Confidence: {probability[0][prediction[0]]*100:.1f}%")


# SUMMARY: THE PATTERN

print("\n" + "=" * 70)
print("SUMMARY: THE UNIVERSAL PATTERN")
print("=" * 70)

summary = """
┌─────────────────────────────────────────────────────────────────────────┐
│                    SCIKIT-LEARN WORKFLOW                                │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│   # 1. Imports                                                          │
│   from sklearn.model_selection import train_test_split                  │
│   from sklearn.preprocessing import StandardScaler                      │
│   from sklearn.linear_model import LogisticRegression                   │
│                                                                         │
│   # 2. Split data                                                       │
│   X_train, X_test, y_train, y_test = train_test_split(X, y)            │
│                                                                         │
│   # 3. Scale features                                                   │
│   scaler = StandardScaler()                                             │
│   X_train = scaler.fit_transform(X_train)                              │
│   X_test = scaler.transform(X_test)                                    │
│                                                                         │
│   # 4. Create and train model                                           │
│   model = LogisticRegression()                                          │
│   model.fit(X_train, y_train)                                          │
│                                                                         │
│   # 5. Evaluate                                                         │
│   print(model.score(X_test, y_test))                                   │
│                                                                         │
│   # 6. Predict                                                          │
│   predictions = model.predict(X_new)                                    │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘

This SAME pattern works for ANY model. Just change the import and model name!
"""
print(summary)

print("\nPart 1 Complete! Run Part 2 for more algorithms and techniques.")
