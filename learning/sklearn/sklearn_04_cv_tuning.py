"""
Scikit-Learn: Zero to Hero
==========================
Part 4: Cross-Validation and Hyperparameter Tuning

How to properly validate your model and find the best settings.
"""

import numpy as np
import matplotlib.pyplot as plt
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier
from sklearn.datasets import load_breast_cancer

print("=" * 70)
print("SCIKIT-LEARN: ZERO TO HERO")
print("Part 4: Cross-Validation and Hyperparameter Tuning")
print("=" * 70)


# ============================================================================
# THE PROBLEM WITH SIMPLE TRAIN/TEST SPLIT
# ============================================================================

print("\n" + "=" * 70)
print("THE PROBLEM WITH SIMPLE TRAIN/TEST SPLIT")
print("=" * 70)

problem = """
Simple train/test split has issues:

1. VARIANCE: Results depend on which data points end up in test set
   - Run twice with different random_state → different scores
   - Is your model good, or did you get a "lucky" split?

2. WASTED DATA: Test set is never used for training
   - With limited data, this hurts model performance

3. OVERFITTING TO TEST SET: If you tune hyperparameters using test set
   - You're indirectly "training" on test set
   - Final evaluation is no longer unbiased

SOLUTION: Cross-Validation
"""
print(problem)


# ============================================================================
# CROSS-VALIDATION EXPLAINED
# ============================================================================

print("\n" + "=" * 70)
print("CROSS-VALIDATION EXPLAINED")
print("=" * 70)

cv_explanation = """
K-FOLD CROSS-VALIDATION:

Split data into K equal parts (folds). Train K times, each time using
a different fold as the test set.

Example: 5-Fold Cross-Validation

     Fold 1    Fold 2    Fold 3    Fold 4    Fold 5
    ┌─────────┬─────────┬─────────┬─────────┬─────────┐
Run 1:  TEST     TRAIN     TRAIN     TRAIN     TRAIN    → Score 1
    ├─────────┼─────────┼─────────┼─────────┼─────────┤
Run 2:  TRAIN     TEST     TRAIN     TRAIN     TRAIN    → Score 2
    ├─────────┼─────────┼─────────┼─────────┼─────────┤
Run 3:  TRAIN    TRAIN      TEST     TRAIN     TRAIN    → Score 3
    ├─────────┼─────────┼─────────┼─────────┼─────────┤
Run 4:  TRAIN    TRAIN     TRAIN      TEST     TRAIN    → Score 4
    ├─────────┼─────────┼─────────┼─────────┼─────────┤
Run 5:  TRAIN    TRAIN     TRAIN     TRAIN      TEST    → Score 5
    └─────────┴─────────┴─────────┴─────────┴─────────┘

Final Score = Average(Score 1, Score 2, Score 3, Score 4, Score 5)

Benefits:
- Every data point gets tested exactly once
- Reduces variance in evaluation
- More reliable estimate of model performance
"""
print(cv_explanation)


# ============================================================================
# CROSS-VALIDATION IN SCIKIT-LEARN
# ============================================================================

print("\n" + "=" * 70)
print("CROSS-VALIDATION IN SCIKIT-LEARN")
print("=" * 70)

# Load data
data = load_breast_cancer()
X, y = data.data, data.target

# Scale features
scaler = StandardScaler()
X_scaled = scaler.fit_transform(X)

# Method 1: cross_val_score (simplest)
print("\n--- Method 1: cross_val_score ---")
from sklearn.model_selection import cross_val_score

model = LogisticRegression(max_iter=1000)
scores = cross_val_score(model, X_scaled, y, cv=5)  # 5-fold CV

print(f"Scores for each fold: {scores}")
print(f"Mean accuracy: {scores.mean():.4f}")
print(f"Std deviation: {scores.std():.4f}")
print(f"95% CI: {scores.mean():.4f} ± {scores.std() * 2:.4f}")

# Method 2: cross_validate (more detailed)
print("\n--- Method 2: cross_validate (multiple metrics) ---")
from sklearn.model_selection import cross_validate

results = cross_validate(
    model, X_scaled, y, cv=5,
    scoring=['accuracy', 'precision', 'recall', 'f1'],
    return_train_score=True
)

print(f"Test Accuracy:  {results['test_accuracy'].mean():.4f} ± {results['test_accuracy'].std():.4f}")
print(f"Test Precision: {results['test_precision'].mean():.4f} ± {results['test_precision'].std():.4f}")
print(f"Test Recall:    {results['test_recall'].mean():.4f} ± {results['test_recall'].std():.4f}")
print(f"Test F1:        {results['test_f1'].mean():.4f} ± {results['test_f1'].std():.4f}")


# ============================================================================
# DIFFERENT CV STRATEGIES
# ============================================================================

print("\n" + "=" * 70)
print("DIFFERENT CV STRATEGIES")
print("=" * 70)

from sklearn.model_selection import (KFold, StratifiedKFold, LeaveOneOut,
                                       ShuffleSplit)

print("""
1. KFold: Basic K-fold, random split
   - Use for: Large datasets, regression

2. StratifiedKFold: Preserves class distribution in each fold
   - Use for: Classification with imbalanced classes

3. LeaveOneOut: Each sample is a test set once (K = N)
   - Use for: Very small datasets

4. ShuffleSplit: Random train/test splits (can repeat samples)
   - Use for: When you want more control over split sizes
""")

# StratifiedKFold example (most common for classification)
print("--- StratifiedKFold Example ---")
skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)

fold_scores = []
for fold, (train_idx, test_idx) in enumerate(skf.split(X_scaled, y)):
    X_train, X_test = X_scaled[train_idx], X_scaled[test_idx]
    y_train, y_test = y[train_idx], y[test_idx]
    
    model = LogisticRegression(max_iter=1000)
    model.fit(X_train, y_train)
    score = model.score(X_test, y_test)
    fold_scores.append(score)
    
    # Show class distribution is preserved
    print(f"Fold {fold+1}: Score={score:.4f}, "
          f"Train class 1: {y_train.mean():.2%}, "
          f"Test class 1: {y_test.mean():.2%}")

print(f"\nMean: {np.mean(fold_scores):.4f}")


# ============================================================================
# HYPERPARAMETER TUNING
# ============================================================================

print("\n" + "=" * 70)
print("HYPERPARAMETER TUNING")
print("=" * 70)

hyperparam_explanation = """
HYPERPARAMETERS vs PARAMETERS:

Parameters: Learned during training (weights θ)
  - model.coef_, model.intercept_

Hyperparameters: Set BEFORE training
  - Learning rate, regularization strength, number of trees, max depth, etc.

Problem: How do you know which hyperparameters are best?
Solution: Try many combinations, evaluate with cross-validation!

WARNING: Don't tune on your test set! You'll overfit to it.

Proper workflow:
┌──────────────────────────────────────────────────────────────────┐
│                        YOUR DATA                                 │
│  ┌────────────────────────────────────┬───────────────────┐      │
│  │         TRAIN + VALIDATION         │     TEST SET      │      │
│  │              (80%)                 │       (20%)       │      │
│  │                                    │                   │      │
│  │   Use cross-validation here to     │   Only use ONCE   │      │
│  │   tune hyperparameters             │   for final eval  │      │
│  └────────────────────────────────────┴───────────────────┘      │
└──────────────────────────────────────────────────────────────────┘
"""
print(hyperparam_explanation)


# ============================================================================
# GRID SEARCH CV
# ============================================================================

print("\n" + "=" * 70)
print("GRID SEARCH CV")
print("=" * 70)

print("""
GridSearchCV: Try ALL combinations of hyperparameters
- Exhaustive but can be slow
- Best when you have few hyperparameters
""")

from sklearn.model_selection import GridSearchCV

# Split off a final test set first!
X_train, X_test, y_train, y_test = train_test_split(
    X_scaled, y, test_size=0.2, random_state=42
)

# Define parameter grid
param_grid = {
    'C': [0.01, 0.1, 1, 10, 100],  # Regularization strength
    'solver': ['lbfgs', 'liblinear']
}

# Create GridSearchCV
grid_search = GridSearchCV(
    LogisticRegression(max_iter=1000),
    param_grid,
    cv=5,               # 5-fold CV for each combination
    scoring='accuracy',
    return_train_score=True,
    n_jobs=-1           # Use all CPU cores
)

# Fit (this trains many models!)
print("Running Grid Search...")
grid_search.fit(X_train, y_train)

print(f"\nBest parameters: {grid_search.best_params_}")
print(f"Best CV score:   {grid_search.best_score_:.4f}")

# Evaluate on held-out test set
test_score = grid_search.score(X_test, y_test)
print(f"Test set score:  {test_score:.4f}")

# Access the best model
best_model = grid_search.best_estimator_


# ============================================================================
# RANDOMIZED SEARCH CV
# ============================================================================

print("\n" + "=" * 70)
print("RANDOMIZED SEARCH CV")
print("=" * 70)

print("""
RandomizedSearchCV: Randomly sample hyperparameter combinations
- Faster than grid search
- Better when you have many hyperparameters
- Can sample from distributions
""")

from sklearn.model_selection import RandomizedSearchCV
from scipy.stats import uniform, randint

# Define parameter distributions
param_distributions = {
    'n_estimators': randint(50, 200),        # Random int between 50-200
    'max_depth': randint(3, 15),             # Random int between 3-15
    'min_samples_split': randint(2, 20),     # Random int between 2-20
    'min_samples_leaf': randint(1, 10)       # Random int between 1-10
}

# Create RandomizedSearchCV
random_search = RandomizedSearchCV(
    RandomForestClassifier(random_state=42),
    param_distributions,
    n_iter=20,          # Try 20 random combinations
    cv=5,
    scoring='accuracy',
    random_state=42,
    n_jobs=-1
)

print("Running Randomized Search...")
random_search.fit(X_train, y_train)

print(f"\nBest parameters: {random_search.best_params_}")
print(f"Best CV score:   {random_search.best_score_:.4f}")
print(f"Test set score:  {random_search.score(X_test, y_test):.4f}")


# ============================================================================
# VIEWING SEARCH RESULTS
# ============================================================================

print("\n" + "=" * 70)
print("VIEWING SEARCH RESULTS")
print("=" * 70)

import pandas as pd

# Convert results to DataFrame
results_df = pd.DataFrame(grid_search.cv_results_)
print("\nGrid Search Results (sorted by rank):")
cols = ['params', 'mean_test_score', 'std_test_score', 'rank_test_score']
print(results_df[cols].sort_values('rank_test_score').head(10).to_string())


# ============================================================================
# THE COMPLETE WORKFLOW
# ============================================================================

print("\n" + "=" * 70)
print("THE COMPLETE WORKFLOW")
print("=" * 70)

workflow = """
┌─────────────────────────────────────────────────────────────────────────┐
│                    PROPER ML WORKFLOW                                   │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│   1. SPLIT DATA                                                         │
│      X_train, X_test, y_train, y_test = train_test_split(...)          │
│      Keep test set completely separate!                                 │
│                                                                         │
│   2. PREPROCESSING                                                      │
│      scaler.fit(X_train)                                               │
│      X_train = scaler.transform(X_train)                               │
│      X_test = scaler.transform(X_test)  # Same transformation!         │
│                                                                         │
│   3. HYPERPARAMETER TUNING (on training data only)                      │
│      grid_search = GridSearchCV(model, param_grid, cv=5)               │
│      grid_search.fit(X_train, y_train)                                 │
│                                                                         │
│   4. GET BEST MODEL                                                     │
│      best_model = grid_search.best_estimator_                          │
│                                                                         │
│   5. FINAL EVALUATION (only once!)                                      │
│      final_score = best_model.score(X_test, y_test)                    │
│                                                                         │
│   6. RETRAIN ON ALL DATA (optional, for deployment)                     │
│      best_model.fit(X_full, y_full)                                    │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
"""
print(workflow)


# ============================================================================
# COMMON HYPERPARAMETERS BY ALGORITHM
# ============================================================================

print("\n" + "=" * 70)
print("COMMON HYPERPARAMETERS BY ALGORITHM")
print("=" * 70)

hyperparams = """
┌─────────────────────────────────────────────────────────────────────────┐
│                 HYPERPARAMETERS TO TUNE                                 │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  LOGISTIC REGRESSION                                                    │
│    C          : Regularization (smaller = stronger regularization)      │
│    penalty    : 'l1', 'l2', 'elasticnet'                               │
│    solver     : 'lbfgs', 'liblinear', 'saga'                           │
│                                                                         │
│  RANDOM FOREST                                                          │
│    n_estimators : Number of trees (more = better, but slower)          │
│    max_depth    : Max tree depth (controls overfitting)                │
│    min_samples_split : Min samples to split a node                     │
│    min_samples_leaf  : Min samples in a leaf                           │
│                                                                         │
│  GRADIENT BOOSTING                                                      │
│    n_estimators  : Number of boosting rounds                           │
│    learning_rate : Step size (lower = more trees needed)               │
│    max_depth     : Tree depth                                          │
│    subsample     : Fraction of samples per tree                        │
│                                                                         │
│  SVM                                                                    │
│    C      : Regularization                                             │
│    kernel : 'linear', 'rbf', 'poly'                                    │
│    gamma  : Kernel coefficient (for rbf)                               │
│                                                                         │
│  KNN                                                                    │
│    n_neighbors : Number of neighbors                                    │
│    weights     : 'uniform', 'distance'                                  │
│    metric      : 'euclidean', 'manhattan'                              │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
"""
print(hyperparams)

print("\nPart 4 Complete! Run Part 5 for Pipelines and Putting It All Together.")
