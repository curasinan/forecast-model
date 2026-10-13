"""Scikit-Learn: Zero to Hero, Part 2: All Major Algorithms."""

import numpy as np
import matplotlib.pyplot as plt
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from sklearn.datasets import make_classification, make_regression, make_blobs

print("=" * 70)
print("SCIKIT-LEARN: ZERO TO HERO")
print("Part 2: All Major Algorithms")
print("=" * 70)


# ALGORITHM MAP: When to Use What

print("\n" + "=" * 70)
print("ALGORITHM MAP: WHEN TO USE WHAT")
print("=" * 70)

algorithm_map = """
┌─────────────────────────────────────────────────────────────────────────┐
│                        WHICH ALGORITHM TO USE?                          │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│   What's your task?                                                     │
│        │                                                                │
│        ├── PREDICT A NUMBER (Regression)                                │
│        │        │                                                       │
│        │        ├── Linear relationship? → Linear Regression            │
│        │        ├── Non-linear? → Decision Tree, Random Forest          │
│        │        └── Complex patterns? → Gradient Boosting, SVR          │
│        │                                                                │
│        ├── PREDICT A CATEGORY (Classification)                          │
│        │        │                                                       │
│        │        ├── 2 classes, linear? → Logistic Regression            │
│        │        ├── 2+ classes, non-linear? → Random Forest, SVM        │
│        │        ├── Need probabilities? → Logistic, Naive Bayes         │
│        │        └── Fast & simple? → K-Nearest Neighbors                │
│        │                                                                │
│        └── FIND GROUPS (Clustering) - No labels!                        │
│                 │                                                       │
│                 ├── Know # of groups? → K-Means                         │
│                 ├── Don't know #? → DBSCAN                              │
│                 └── Hierarchical? → Agglomerative Clustering            │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
"""
print(algorithm_map)


# SECTION A: REGRESSION ALGORITHMS

print("\n" + "=" * 70)
print("SECTION A: REGRESSION ALGORITHMS")
print("Predict continuous values (prices, temperatures, scores)")
print("=" * 70)

# Generate regression data
X_reg, y_reg = make_regression(n_samples=200, n_features=1, noise=20, random_state=42)
X_train, X_test, y_train, y_test = train_test_split(X_reg, y_reg, test_size=0.2, random_state=42)

print("\n--- 1. Linear Regression ---")
print("Use when: Linear relationship between features and target")
print("Pros: Fast, interpretable, no hyperparameters")
print("Cons: Can't capture non-linear patterns")

from sklearn.linear_model import LinearRegression

model = LinearRegression()
model.fit(X_train, y_train)
print(f"R² Score: {model.score(X_test, y_test):.4f}")


print("\n--- 2. Ridge Regression (L2 Regularization) ---")
print("Use when: Many features, want to prevent overfitting")
print("Pros: Reduces overfitting, handles multicollinearity")
print("Cons: Need to tune alpha parameter")

from sklearn.linear_model import Ridge

model = Ridge(alpha=1.0)  # alpha controls regularization strength
model.fit(X_train, y_train)
print(f"R² Score: {model.score(X_test, y_test):.4f}")


print("\n--- 3. Lasso Regression (L1 Regularization) ---")
print("Use when: Want automatic feature selection")
print("Pros: Can reduce coefficients to exactly 0 (feature selection)")
print("Cons: Can be unstable with correlated features")

from sklearn.linear_model import Lasso

model = Lasso(alpha=1.0)
model.fit(X_train, y_train)
print(f"R² Score: {model.score(X_test, y_test):.4f}")


print("\n--- 4. Decision Tree Regressor ---")
print("Use when: Non-linear relationships, want interpretability")
print("Pros: Handles non-linearity, easy to visualize")
print("Cons: Prone to overfitting")

from sklearn.tree import DecisionTreeRegressor

model = DecisionTreeRegressor(max_depth=5, random_state=42)
model.fit(X_train, y_train)
print(f"R² Score: {model.score(X_test, y_test):.4f}")


print("\n--- 5. Random Forest Regressor ---")
print("Use when: Want accuracy + handles non-linearity")
print("Pros: Reduces overfitting vs single tree, robust")
print("Cons: Slower, less interpretable")

from sklearn.ensemble import RandomForestRegressor

model = RandomForestRegressor(n_estimators=100, max_depth=5, random_state=42)
model.fit(X_train, y_train)
print(f"R² Score: {model.score(X_test, y_test):.4f}")


print("\n--- 6. Gradient Boosting Regressor ---")
print("Use when: Want best accuracy, have time to tune")
print("Pros: Often best performance")
print("Cons: Slower, many hyperparameters")

from sklearn.ensemble import GradientBoostingRegressor

model = GradientBoostingRegressor(n_estimators=100, max_depth=3, random_state=42)
model.fit(X_train, y_train)
print(f"R² Score: {model.score(X_test, y_test):.4f}")


print("\n--- 7. Support Vector Regressor (SVR) ---")
print("Use when: Non-linear data, medium-sized datasets")
print("Pros: Effective in high dimensions")
print("Cons: Slow on large datasets, needs scaling")

from sklearn.svm import SVR

scaler = StandardScaler()
X_train_scaled = scaler.fit_transform(X_train)
X_test_scaled = scaler.transform(X_test)

model = SVR(kernel='rbf')
model.fit(X_train_scaled, y_train)
print(f"R² Score: {model.score(X_test_scaled, y_test):.4f}")


# SECTION B: CLASSIFICATION ALGORITHMS

print("\n" + "=" * 70)
print("SECTION B: CLASSIFICATION ALGORITHMS")
print("Predict categories (spam/not spam, disease/healthy)")
print("=" * 70)

# Generate classification data
X_clf, y_clf = make_classification(n_samples=300, n_features=10, n_informative=5,
                                    n_redundant=2, random_state=42)
X_train, X_test, y_train, y_test = train_test_split(X_clf, y_clf, test_size=0.2, random_state=42)

# Scale for algorithms that need it
scaler = StandardScaler()
X_train_scaled = scaler.fit_transform(X_train)
X_test_scaled = scaler.transform(X_test)


print("\n--- 1. Logistic Regression ---")
print("Use when: Binary classification, need probabilities, linear boundary")
print("Pros: Fast, interpretable, gives probabilities")
print("Cons: Assumes linear decision boundary")

from sklearn.linear_model import LogisticRegression

model = LogisticRegression(max_iter=1000)
model.fit(X_train_scaled, y_train)
print(f"Accuracy: {model.score(X_test_scaled, y_test):.4f}")


print("\n--- 2. K-Nearest Neighbors (KNN) ---")
print("Use when: Simple problems, want instance-based learning")
print("Pros: No training needed, intuitive")
print("Cons: Slow prediction, needs scaling, memory intensive")

from sklearn.neighbors import KNeighborsClassifier

model = KNeighborsClassifier(n_neighbors=5)
model.fit(X_train_scaled, y_train)
print(f"Accuracy: {model.score(X_test_scaled, y_test):.4f}")


print("\n--- 3. Support Vector Machine (SVM) ---")
print("Use when: Medium datasets, clear margin of separation")
print("Pros: Effective in high dimensions, memory efficient")
print("Cons: Slow on large datasets, needs scaling")

from sklearn.svm import SVC

model = SVC(kernel='rbf', probability=True)  # probability=True for predict_proba
model.fit(X_train_scaled, y_train)
print(f"Accuracy: {model.score(X_test_scaled, y_test):.4f}")


print("\n--- 4. Decision Tree Classifier ---")
print("Use when: Need interpretability, mixed feature types")
print("Pros: Easy to understand, no scaling needed")
print("Cons: Prone to overfitting")

from sklearn.tree import DecisionTreeClassifier

model = DecisionTreeClassifier(max_depth=5, random_state=42)
model.fit(X_train, y_train)  # No scaling needed for trees!
print(f"Accuracy: {model.score(X_test, y_test):.4f}")


print("\n--- 5. Random Forest Classifier ---")
print("Use when: Want accuracy + robustness")
print("Pros: Reduces overfitting, handles non-linearity")
print("Cons: Less interpretable, slower")

from sklearn.ensemble import RandomForestClassifier

model = RandomForestClassifier(n_estimators=100, max_depth=5, random_state=42)
model.fit(X_train, y_train)
print(f"Accuracy: {model.score(X_test, y_test):.4f}")


print("\n--- 6. Gradient Boosting Classifier ---")
print("Use when: Want best accuracy")
print("Pros: Often top performance")
print("Cons: Slower, prone to overfitting if not tuned")

from sklearn.ensemble import GradientBoostingClassifier

model = GradientBoostingClassifier(n_estimators=100, max_depth=3, random_state=42)
model.fit(X_train, y_train)
print(f"Accuracy: {model.score(X_test, y_test):.4f}")


print("\n--- 7. Naive Bayes ---")
print("Use when: Text classification, need speed, small dataset")
print("Pros: Very fast, works with small data")
print("Cons: Assumes feature independence (naive)")

from sklearn.naive_bayes import GaussianNB

model = GaussianNB()
model.fit(X_train, y_train)
print(f"Accuracy: {model.score(X_test, y_test):.4f}")


print("\n--- 8. Perceptron ---")
print("Use when: Educational purposes, very large datasets")
print("Pros: Simple, fast")
print("Cons: Only linear boundaries, no probabilities")

from sklearn.linear_model import Perceptron

model = Perceptron(max_iter=1000, random_state=42)
model.fit(X_train_scaled, y_train)
print(f"Accuracy: {model.score(X_test_scaled, y_test):.4f}")


# SECTION C: CLUSTERING ALGORITHMS (Unsupervised)

print("\n" + "=" * 70)
print("SECTION C: CLUSTERING ALGORITHMS")
print("Find groups in data WITHOUT labels (unsupervised)")
print("=" * 70)

# Generate clustering data
X_cluster, y_true = make_blobs(n_samples=300, centers=4, cluster_std=0.6, random_state=42)


print("\n--- 1. K-Means ---")
print("Use when: Know the number of clusters, spherical clusters")
print("Pros: Fast, scales well")
print("Cons: Must specify K, assumes spherical clusters")

from sklearn.cluster import KMeans

model = KMeans(n_clusters=4, random_state=42, n_init=10)
model.fit(X_cluster)
print(f"Cluster labels: {np.unique(model.labels_)}")
print(f"Cluster centers shape: {model.cluster_centers_.shape}")


print("\n--- 2. DBSCAN ---")
print("Use when: Don't know # of clusters, have noise, non-spherical")
print("Pros: Finds arbitrary shapes, detects outliers")
print("Cons: Sensitive to parameters, struggles with varying density")

from sklearn.cluster import DBSCAN

model = DBSCAN(eps=0.5, min_samples=5)
model.fit(X_cluster)
n_clusters = len(set(model.labels_)) - (1 if -1 in model.labels_ else 0)
print(f"Found {n_clusters} clusters")
print(f"Noise points: {(model.labels_ == -1).sum()}")


print("\n--- 3. Agglomerative Clustering ---")
print("Use when: Want hierarchical structure, dendrogram visualization")
print("Pros: No need to specify K upfront, hierarchical")
print("Cons: Slow on large datasets")

from sklearn.cluster import AgglomerativeClustering

model = AgglomerativeClustering(n_clusters=4)
labels = model.fit_predict(X_cluster)
print(f"Cluster labels: {np.unique(labels)}")


# SECTION D: DIMENSIONALITY REDUCTION

print("\n" + "=" * 70)
print("SECTION D: DIMENSIONALITY REDUCTION")
print("Reduce features while preserving information")
print("=" * 70)


print("\n--- 1. PCA (Principal Component Analysis) ---")
print("Use when: Too many features, want to visualize high-dim data")
print("Pros: Removes correlation, speeds up training")
print("Cons: Linear only, less interpretable features")

from sklearn.decomposition import PCA

# High-dimensional data
X_high_dim = np.random.randn(100, 50)  # 50 features

pca = PCA(n_components=2)  # Reduce to 2 dimensions
X_reduced = pca.fit_transform(X_high_dim)

print(f"Original shape: {X_high_dim.shape}")
print(f"Reduced shape:  {X_reduced.shape}")
print(f"Variance explained: {pca.explained_variance_ratio_.sum():.2%}")


print("\n--- 2. t-SNE ---")
print("Use when: Visualizing high-dimensional data in 2D/3D")
print("Pros: Great for visualization")
print("Cons: Slow, can't transform new data, non-deterministic")

from sklearn.manifold import TSNE

tsne = TSNE(n_components=2, random_state=42, perplexity=30)
X_tsne = tsne.fit_transform(X_high_dim)
print(f"t-SNE output shape: {X_tsne.shape}")


# ALGORITHM COMPARISON SUMMARY

print("\n" + "=" * 70)
print("ALGORITHM COMPARISON SUMMARY")
print("=" * 70)

summary = """
┌─────────────────────────────────────────────────────────────────────────┐
│                    ALGORITHM QUICK REFERENCE                            │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  REGRESSION (predict numbers)                                           │
│  ─────────────────────────────                                          │
│  LinearRegression      Simple, fast, interpretable                      │
│  Ridge/Lasso           When you have many features (regularization)     │
│  DecisionTreeRegressor Non-linear, interpretable                        │
│  RandomForestRegressor Non-linear, robust, accurate                     │
│  GradientBoostingRegressor  Best accuracy, slower                       │
│  SVR                   Non-linear, needs scaling                        │
│                                                                         │
│  CLASSIFICATION (predict categories)                                    │
│  ───────────────────────────────────                                    │
│  LogisticRegression    Binary, probabilities, fast                      │
│  KNeighborsClassifier  Simple, no training, needs scaling               │
│  SVC                   Complex boundaries, needs scaling                │
│  DecisionTreeClassifier Interpretable, no scaling needed                │
│  RandomForestClassifier Robust, accurate, handles non-linearity         │
│  GradientBoostingClassifier Best accuracy, needs tuning                 │
│  GaussianNB            Fast, text classification                        │
│                                                                         │
│  CLUSTERING (find groups, no labels)                                    │
│  ───────────────────────────────────                                    │
│  KMeans                Know # clusters, fast                            │
│  DBSCAN                Unknown # clusters, finds outliers               │
│  AgglomerativeClustering Hierarchical structure                         │
│                                                                         │
│  DIMENSIONALITY REDUCTION                                               │
│  ────────────────────────                                               │
│  PCA                   Reduce features, preprocessing                   │
│  t-SNE                 Visualization only                               │
│                                                                         │
├─────────────────────────────────────────────────────────────────────────┤
│  NEED SCALING?                                                          │
│  ─────────────                                                          │
│  YES: LogisticRegression, KNN, SVM, Perceptron, PCA                     │
│  NO:  Decision Trees, Random Forest, Gradient Boosting, Naive Bayes    │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
"""
print(summary)

print("\nPart 2 Complete! Run Part 3 for Model Evaluation and Metrics.")
