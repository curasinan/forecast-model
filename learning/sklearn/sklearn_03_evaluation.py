"""Scikit-Learn: Zero to Hero, Part 3: Model Evaluation and Metrics."""

import numpy as np
import matplotlib.pyplot as plt
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
from sklearn.datasets import make_classification

print("=" * 70)
print("SCIKIT-LEARN: ZERO TO HERO")
print("Part 3: Model Evaluation and Metrics")
print("=" * 70)


# WHY ACCURACY ISN'T ENOUGH

print("\n" + "=" * 70)
print("WHY ACCURACY ISN'T ENOUGH")
print("=" * 70)

example = """
Imagine: Disease detection dataset
- 1000 patients
- 990 healthy (99%)
- 10 have disease (1%)

A stupid model that ALWAYS predicts "healthy":
- Accuracy = 990/1000 = 99%  ← Looks great!
- But it misses ALL sick patients! ← Terrible in practice

This is called the CLASS IMBALANCE problem.
We need better metrics than just accuracy.
"""
print(example)


# Generate imbalanced data for demonstration
X, y = make_classification(n_samples=1000, n_features=10, n_informative=5,
                            n_classes=2, weights=[0.9, 0.1],  # Imbalanced!
                            random_state=42)
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

print(f"\nOur imbalanced dataset:")
print(f"  Class 0: {(y == 0).sum()} samples ({(y == 0).mean()*100:.1f}%)")
print(f"  Class 1: {(y == 1).sum()} samples ({(y == 1).mean()*100:.1f}%)")

# Train a model
model = LogisticRegression(max_iter=1000)
model.fit(X_train, y_train)
y_pred = model.predict(X_test)


# CONFUSION MATRIX: The Foundation

print("\n" + "=" * 70)
print("CONFUSION MATRIX: THE FOUNDATION")
print("=" * 70)

confusion_explanation = """
The confusion matrix shows what your model got right and wrong:

                          PREDICTED
                    │  Negative  │  Positive  │
              ──────┼────────────┼────────────┤
    ACTUAL Negative │     TN     │     FP     │   ← Actually negative
              ──────┼────────────┼────────────┤
    ACTUAL Positive │     FN     │     TP     │   ← Actually positive
              ──────┼────────────┼────────────┤

TN (True Negative):  Correctly predicted negative
FP (False Positive): Incorrectly predicted positive (Type I error)
FN (False Negative): Incorrectly predicted negative (Type II error)
TP (True Positive):  Correctly predicted positive

Example: Disease detection
- TP: Correctly identified sick patient
- TN: Correctly identified healthy patient
- FP: Healthy patient incorrectly told they're sick (unnecessary worry)
- FN: Sick patient incorrectly told they're healthy (DANGEROUS!)
"""
print(confusion_explanation)

from sklearn.metrics import confusion_matrix, ConfusionMatrixDisplay

cm = confusion_matrix(y_test, y_pred)
print(f"Confusion Matrix:")
print(f"  [[TN={cm[0,0]:3d}, FP={cm[0,1]:3d}]")
print(f"   [FN={cm[1,0]:3d}, TP={cm[1,1]:3d}]]")


# KEY METRICS FOR CLASSIFICATION

print("\n" + "=" * 70)
print("KEY METRICS FOR CLASSIFICATION")
print("=" * 70)

from sklearn.metrics import (accuracy_score, precision_score, recall_score, 
                              f1_score, classification_report)

print("""
1. ACCURACY = (TP + TN) / Total
   "Of all predictions, how many were correct?"
   Problem: Misleading with imbalanced data
""")
accuracy = accuracy_score(y_test, y_pred)
print(f"   Accuracy: {accuracy:.4f}")

print("""
2. PRECISION = TP / (TP + FP)
   "Of all POSITIVE predictions, how many were actually positive?"
   Use when: False positives are costly (spam detection)
""")
precision = precision_score(y_test, y_pred)
print(f"   Precision: {precision:.4f}")

print("""
3. RECALL (Sensitivity) = TP / (TP + FN)
   "Of all ACTUAL positives, how many did we find?"
   Use when: False negatives are costly (disease detection)
""")
recall = recall_score(y_test, y_pred)
print(f"   Recall: {recall:.4f}")

print("""
4. F1 SCORE = 2 × (Precision × Recall) / (Precision + Recall)
   Harmonic mean of precision and recall
   Use when: You need balance between precision and recall
""")
f1 = f1_score(y_test, y_pred)
print(f"   F1 Score: {f1:.4f}")

print("\n5. CLASSIFICATION REPORT (All metrics at once):")
print(classification_report(y_test, y_pred))


# PRECISION vs RECALL TRADEOFF

print("\n" + "=" * 70)
print("PRECISION vs RECALL TRADEOFF")
print("=" * 70)

tradeoff = """
You often can't maximize both precision AND recall.

Higher threshold (more conservative):
  ├── Higher Precision (fewer false positives)
  └── Lower Recall (miss more actual positives)

Lower threshold (more aggressive):
  ├── Lower Precision (more false positives)
  └── Higher Recall (catch more actual positives)

Example scenarios:
┌─────────────────────────────────────────────────────────────────────────┐
│ Scenario              │ Optimize For │ Why                             │
├─────────────────────────────────────────────────────────────────────────┤
│ Disease detection     │ RECALL       │ Missing sick patient is worse   │
│                       │              │ than false alarm                │
├─────────────────────────────────────────────────────────────────────────┤
│ Spam filter           │ PRECISION    │ Missing spam is OK, but putting │
│                       │              │ real email in spam is bad       │
├─────────────────────────────────────────────────────────────────────────┤
│ Fraud detection       │ RECALL       │ Missing fraud is costly         │
├─────────────────────────────────────────────────────────────────────────┤
│ Search engine results │ PRECISION    │ Top results must be relevant    │
└─────────────────────────────────────────────────────────────────────────┘
"""
print(tradeoff)


# ROC CURVE AND AUC

print("\n" + "=" * 70)
print("ROC CURVE AND AUC")
print("=" * 70)

print("""
ROC (Receiver Operating Characteristic) Curve:
- Plots True Positive Rate vs False Positive Rate at different thresholds
- AUC (Area Under Curve) summarizes the curve in one number

AUC Interpretation:
- 1.0 = Perfect classifier
- 0.5 = Random guessing (diagonal line)
- < 0.5 = Worse than random

AUC is useful because it's threshold-independent.
""")

from sklearn.metrics import roc_curve, roc_auc_score

# Get probabilities (not just predictions)
y_prob = model.predict_proba(X_test)[:, 1]

# Calculate ROC curve
fpr, tpr, thresholds = roc_curve(y_test, y_prob)
auc = roc_auc_score(y_test, y_prob)

print(f"AUC Score: {auc:.4f}")

# Plot ROC curve
plt.figure(figsize=(8, 6))
plt.plot(fpr, tpr, label=f'ROC Curve (AUC = {auc:.3f})')
plt.plot([0, 1], [0, 1], 'k--', label='Random Guessing')
plt.xlabel('False Positive Rate')
plt.ylabel('True Positive Rate')
plt.title('ROC Curve')
plt.legend()
plt.grid(True, alpha=0.3)
plt.savefig('roc_curve.png', dpi=150, bbox_inches='tight')
plt.close()
print("✓ Saved ROC curve to 'roc_curve.png'")


# METRICS FOR REGRESSION

print("\n" + "=" * 70)
print("METRICS FOR REGRESSION")
print("=" * 70)

from sklearn.datasets import make_regression
from sklearn.linear_model import LinearRegression
from sklearn.metrics import (mean_squared_error, mean_absolute_error, 
                              r2_score, root_mean_squared_error)

# Generate regression data
X_reg, y_reg = make_regression(n_samples=100, n_features=1, noise=20, random_state=42)
X_train_r, X_test_r, y_train_r, y_test_r = train_test_split(X_reg, y_reg, test_size=0.2)

model_reg = LinearRegression()
model_reg.fit(X_train_r, y_train_r)
y_pred_r = model_reg.predict(X_test_r)

print("""
1. MSE (Mean Squared Error) = (1/n) × Σ(y - ŷ)²
   Average of squared errors
   Penalizes large errors more heavily
""")
mse = mean_squared_error(y_test_r, y_pred_r)
print(f"   MSE: {mse:.4f}")

print("""
2. RMSE (Root Mean Squared Error) = √MSE
   Same units as the target variable
   Easier to interpret than MSE
""")
rmse = root_mean_squared_error(y_test_r, y_pred_r)
print(f"   RMSE: {rmse:.4f}")

print("""
3. MAE (Mean Absolute Error) = (1/n) × Σ|y - ŷ|
   Average absolute error
   Less sensitive to outliers than MSE
""")
mae = mean_absolute_error(y_test_r, y_pred_r)
print(f"   MAE: {mae:.4f}")

print("""
4. R² (Coefficient of Determination)
   Proportion of variance explained by model
   1.0 = perfect, 0.0 = predicts mean, <0 = worse than mean
""")
r2 = r2_score(y_test_r, y_pred_r)
print(f"   R²: {r2:.4f}")


# CHOOSING THE RIGHT METRIC

print("\n" + "=" * 70)
print("CHOOSING THE RIGHT METRIC")
print("=" * 70)

choosing = """
┌─────────────────────────────────────────────────────────────────────────┐
│                    WHICH METRIC TO USE?                                 │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  CLASSIFICATION                                                         │
│  ──────────────                                                         │
│  Balanced classes + overall performance    → Accuracy                   │
│  Imbalanced classes                        → F1, AUC, Precision-Recall  │
│  False positives are costly                → Precision                  │
│  False negatives are costly                → Recall                     │
│  Need probability ranking                  → AUC                        │
│  Multi-class                               → Macro/Micro F1             │
│                                                                         │
│  REGRESSION                                                             │
│  ──────────────                                                         │
│  General performance                       → R², RMSE                   │
│  Interpretable units                       → MAE, RMSE                  │
│  Sensitive to outliers                     → MSE, RMSE                  │
│  Robust to outliers                        → MAE                        │
│  Compare different models                  → R²                         │
│                                                                         │
│  CLUSTERING (harder to evaluate)                                        │
│  ──────────────                                                         │
│  Have ground truth labels                  → Adjusted Rand Index        │
│  No ground truth                           → Silhouette Score           │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
"""
print(choosing)


# MULTI-CLASS METRICS

print("\n" + "=" * 70)
print("MULTI-CLASS METRICS")
print("=" * 70)

print("""
When you have more than 2 classes, metrics need averaging:

1. MACRO: Calculate metric for each class, then average
   - Treats all classes equally
   - Good when all classes are equally important

2. MICRO: Calculate globally (total TP, FP, FN)
   - Weighted by class frequency
   - Good when you care about overall performance

3. WEIGHTED: Like macro, but weighted by class support
   - Accounts for class imbalance

Example:
  precision_score(y_true, y_pred, average='macro')
  precision_score(y_true, y_pred, average='micro')
  precision_score(y_true, y_pred, average='weighted')
""")

# Multi-class example
from sklearn.datasets import load_iris
from sklearn.ensemble import RandomForestClassifier

iris = load_iris()
X_train_i, X_test_i, y_train_i, y_test_i = train_test_split(
    iris.data, iris.target, test_size=0.2, random_state=42
)

model_mc = RandomForestClassifier(n_estimators=100, random_state=42)
model_mc.fit(X_train_i, y_train_i)
y_pred_i = model_mc.predict(X_test_i)

print("\nMulti-class classification report:")
print(classification_report(y_test_i, y_pred_i, target_names=iris.target_names))


# SUMMARY: METRICS CHEAT SHEET

print("\n" + "=" * 70)
print("SUMMARY: METRICS CHEAT SHEET")
print("=" * 70)

summary = """
┌─────────────────────────────────────────────────────────────────────────┐
│                       METRICS CHEAT SHEET                               │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  from sklearn.metrics import:                                           │
│                                                                         │
│  CLASSIFICATION:                                                        │
│    accuracy_score(y_true, y_pred)                                       │
│    precision_score(y_true, y_pred)                                      │
│    recall_score(y_true, y_pred)                                         │
│    f1_score(y_true, y_pred)                                             │
│    roc_auc_score(y_true, y_prob)     # Needs probabilities              │
│    confusion_matrix(y_true, y_pred)                                     │
│    classification_report(y_true, y_pred)  # All at once                 │
│                                                                         │
│  REGRESSION:                                                            │
│    mean_squared_error(y_true, y_pred)                                   │
│    root_mean_squared_error(y_true, y_pred)                              │
│    mean_absolute_error(y_true, y_pred)                                  │
│    r2_score(y_true, y_pred)                                             │
│                                                                         │
│  CLUSTERING:                                                            │
│    silhouette_score(X, labels)       # No ground truth needed           │
│    adjusted_rand_score(y_true, y_pred)  # Needs ground truth            │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
"""
print(summary)

print("\nPart 3 Complete! Run Part 4 for Cross-Validation and Hyperparameter Tuning.")
