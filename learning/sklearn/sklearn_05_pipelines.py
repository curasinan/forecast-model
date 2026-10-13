"""Scikit-Learn: Zero to Hero, Part 5: Pipelines and Complete Workflow."""

import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split, GridSearchCV, cross_val_score
from sklearn.preprocessing import StandardScaler, LabelEncoder, OneHotEncoder
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression

print("=" * 70)
print("SCIKIT-LEARN: ZERO TO HERO")
print("Part 5: Pipelines and Complete Workflow")
print("=" * 70)


# WHY PIPELINES?

print("\n" + "=" * 70)
print("WHY PIPELINES?")
print("=" * 70)

why_pipelines = """
WITHOUT Pipelines (error-prone):

    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    X_test_scaled = scaler.transform(X_test)    # Easy to forget .transform!
    
    model = LogisticRegression()
    model.fit(X_train_scaled, y_train)
    
    # For new data, must remember to scale first!
    X_new_scaled = scaler.transform(X_new)
    predictions = model.predict(X_new_scaled)

Problems:
1. Must manually track multiple objects (scaler, model)
2. Easy to forget steps
3. Data leakage if you fit scaler on test data by accident
4. Can't easily use with GridSearchCV


WITH Pipelines (clean and safe):

    pipeline = Pipeline([
        ('scaler', StandardScaler()),
        ('model', LogisticRegression())
    ])
    
    pipeline.fit(X_train, y_train)
    predictions = pipeline.predict(X_new)  # Automatically scales!

Benefits:
1. One object to track
2. Impossible to forget steps
3. Prevents data leakage automatically
4. Works seamlessly with GridSearchCV
"""
print(why_pipelines)


# BASIC PIPELINE

print("\n" + "=" * 70)
print("BASIC PIPELINE")
print("=" * 70)

from sklearn.datasets import load_breast_cancer
from sklearn.pipeline import Pipeline

# Load data
data = load_breast_cancer()
X, y = data.data, data.target
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

# Create pipeline
pipeline = Pipeline([
    ('scaler', StandardScaler()),           # Step 1: Scale
    ('classifier', LogisticRegression(max_iter=1000))  # Step 2: Classify
])

print("Pipeline structure:")
print(pipeline)

# Fit the entire pipeline
pipeline.fit(X_train, y_train)

# Evaluate
train_score = pipeline.score(X_train, y_train)
test_score = pipeline.score(X_test, y_test)

print(f"\nTraining accuracy: {train_score:.4f}")
print(f"Test accuracy:     {test_score:.4f}")

# Predict (automatically scales the data!)
sample = X_test[0:3]
predictions = pipeline.predict(sample)
print(f"\nPredictions for 3 samples: {predictions}")


# PIPELINE WITH GRID SEARCH

print("\n" + "=" * 70)
print("PIPELINE WITH GRID SEARCH")
print("=" * 70)

print("""
The magic: GridSearchCV can tune hyperparameters of ANY step in the pipeline!

Use 'stepname__parameter' syntax:
- 'classifier__C' = C parameter of the classifier step
- 'scaler__with_mean' = with_mean parameter of scaler step
""")

# Define parameter grid
param_grid = {
    'classifier__C': [0.01, 0.1, 1, 10],
    'classifier__solver': ['lbfgs', 'liblinear']
}

# Grid search on pipeline
grid_search = GridSearchCV(
    pipeline,
    param_grid,
    cv=5,
    scoring='accuracy',
    n_jobs=-1
)

print("Running Grid Search on Pipeline...")
grid_search.fit(X_train, y_train)

print(f"\nBest parameters: {grid_search.best_params_}")
print(f"Best CV score:   {grid_search.best_score_:.4f}")
print(f"Test score:      {grid_search.score(X_test, y_test):.4f}")


# HANDLING MIXED DATA TYPES

print("\n" + "=" * 70)
print("HANDLING MIXED DATA TYPES")
print("=" * 70)

print("""
Real data often has:
- Numerical features (age, income) → Scale them
- Categorical features (color, country) → Encode them
- Missing values → Impute them

ColumnTransformer lets you apply different transformations to different columns!
""")

# Create sample mixed data
df = pd.DataFrame({
    'age': [25, 30, None, 45, 35, 28, None, 50],
    'income': [50000, 60000, 75000, None, 80000, 55000, 70000, 90000],
    'education': ['bachelors', 'masters', 'phd', 'bachelors', 'masters', 
                  'bachelors', 'phd', 'masters'],
    'employed': ['yes', 'yes', 'no', 'yes', 'no', 'yes', 'yes', 'no'],
    'will_buy': [0, 1, 1, 0, 1, 0, 1, 1]
})

print("Sample mixed data:")
print(df)

X = df.drop('will_buy', axis=1)
y = df['will_buy']

# Define column types
numerical_features = ['age', 'income']
categorical_features = ['education', 'employed']

# Create preprocessor
preprocessor = ColumnTransformer(
    transformers=[
        ('num', Pipeline([
            ('imputer', SimpleImputer(strategy='mean')),  # Fill missing with mean
            ('scaler', StandardScaler())                   # Scale
        ]), numerical_features),
        
        ('cat', Pipeline([
            ('imputer', SimpleImputer(strategy='most_frequent')),  # Fill with mode
            ('encoder', OneHotEncoder(handle_unknown='ignore'))    # One-hot encode
        ]), categorical_features)
    ]
)

# Full pipeline
full_pipeline = Pipeline([
    ('preprocessor', preprocessor),
    ('classifier', RandomForestClassifier(n_estimators=100, random_state=42))
])

print("\nFull pipeline structure:")
print(full_pipeline)

# Fit and transform
full_pipeline.fit(X, y)
print(f"\nPipeline trained successfully!")

# Show what preprocessing does
X_transformed = preprocessor.fit_transform(X)
print(f"\nOriginal shape: {X.shape}")
print(f"Transformed shape: {X_transformed.shape}")


# FEATURE ENCODING EXPLAINED

print("\n" + "=" * 70)
print("FEATURE ENCODING EXPLAINED")
print("=" * 70)

encoding = """
NUMERICAL FEATURES: Usually just scale them
    StandardScaler: mean=0, std=1
    MinMaxScaler: range [0, 1]

CATEGORICAL FEATURES: Must convert to numbers

1. LABEL ENCODING: Convert to integers
   ['red', 'green', 'blue'] → [0, 1, 2]
   Problem: Implies order (2 > 1 > 0) when there is none!
   Use for: Target variable (y), ordinal categories

2. ONE-HOT ENCODING: Create binary columns for each category
   color = 'red'   → [1, 0, 0] (columns: is_red, is_green, is_blue)
   color = 'green' → [0, 1, 0]
   color = 'blue'  → [0, 0, 1]
   Use for: Nominal categories (no order)

3. ORDINAL ENCODING: Convert to integers with meaningful order
   ['small', 'medium', 'large'] → [0, 1, 2]
   Use for: Categories with natural order
"""
print(encoding)

from sklearn.preprocessing import LabelEncoder, OneHotEncoder, OrdinalEncoder

# Label Encoding example
le = LabelEncoder()
colors = ['red', 'green', 'blue', 'red', 'blue']
encoded = le.fit_transform(colors)
print(f"Label Encoding: {colors}")
print(f"              → {encoded}")

# One-Hot Encoding example
ohe = OneHotEncoder(sparse_output=False)
colors_2d = np.array(colors).reshape(-1, 1)
one_hot = ohe.fit_transform(colors_2d)
print(f"\nOne-Hot Encoding:")
print(f"Categories: {ohe.categories_[0]}")
print(one_hot)


# HANDLING MISSING VALUES

print("\n" + "=" * 70)
print("HANDLING MISSING VALUES")
print("=" * 70)

from sklearn.impute import SimpleImputer

impute_explanation = """
Strategies for missing data:

1. DROP: Remove rows/columns with missing values
   df.dropna()
   Problem: Loses data

2. IMPUTE: Fill missing values with estimated values
   SimpleImputer(strategy='mean')     # Numerical: fill with mean
   SimpleImputer(strategy='median')   # Numerical: fill with median (robust to outliers)
   SimpleImputer(strategy='most_frequent')  # Categorical: fill with mode
   SimpleImputer(strategy='constant', fill_value=0)  # Fill with specific value
"""
print(impute_explanation)

# Example
data_with_missing = np.array([[1, 2, np.nan],
                               [4, np.nan, 6],
                               [7, 8, 9],
                               [np.nan, 11, 12]])

print(f"Data with missing values:")
print(data_with_missing)

imputer = SimpleImputer(strategy='mean')
imputed = imputer.fit_transform(data_with_missing)
print(f"\nAfter mean imputation:")
print(imputed)


# COMPLETE REAL-WORLD EXAMPLE

print("\n" + "=" * 70)
print("COMPLETE REAL-WORLD EXAMPLE")
print("=" * 70)

# Create realistic dataset
np.random.seed(42)
n_samples = 500

df_real = pd.DataFrame({
    'age': np.random.randint(18, 70, n_samples),
    'income': np.random.randint(20000, 150000, n_samples),
    'credit_score': np.random.randint(300, 850, n_samples),
    'employment_years': np.random.randint(0, 40, n_samples),
    'education': np.random.choice(['high_school', 'bachelors', 'masters', 'phd'], n_samples),
    'loan_purpose': np.random.choice(['home', 'car', 'education', 'personal'], n_samples),
    'approved': np.random.randint(0, 2, n_samples)
})

# Add some missing values
df_real.loc[np.random.choice(n_samples, 30), 'income'] = np.nan
df_real.loc[np.random.choice(n_samples, 20), 'credit_score'] = np.nan

print("Loan Approval Dataset:")
print(df_real.head(10))
print(f"\nMissing values:\n{df_real.isnull().sum()}")

# Prepare data
X = df_real.drop('approved', axis=1)
y = df_real['approved']

# Split
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

# Define columns
num_features = ['age', 'income', 'credit_score', 'employment_years']
cat_features = ['education', 'loan_purpose']

# Build complete pipeline
complete_pipeline = Pipeline([
    ('preprocessor', ColumnTransformer([
        ('numerical', Pipeline([
            ('imputer', SimpleImputer(strategy='median')),
            ('scaler', StandardScaler())
        ]), num_features),
        ('categorical', Pipeline([
            ('imputer', SimpleImputer(strategy='most_frequent')),
            ('encoder', OneHotEncoder(handle_unknown='ignore'))
        ]), cat_features)
    ])),
    ('classifier', RandomForestClassifier(random_state=42))
])

# Define hyperparameters to tune
param_grid = {
    'classifier__n_estimators': [50, 100, 200],
    'classifier__max_depth': [5, 10, 15, None],
    'classifier__min_samples_split': [2, 5, 10]
}

# Grid search
print("\nRunning Grid Search...")
grid_search = GridSearchCV(
    complete_pipeline,
    param_grid,
    cv=5,
    scoring='accuracy',
    n_jobs=-1,
    verbose=1
)

grid_search.fit(X_train, y_train)

print(f"\nBest parameters: {grid_search.best_params_}")
print(f"Best CV accuracy: {grid_search.best_score_:.4f}")
print(f"Test accuracy:    {grid_search.score(X_test, y_test):.4f}")

# The final model can be saved and used for production
best_pipeline = grid_search.best_estimator_


# SAVING AND LOADING MODELS

print("\n" + "=" * 70)
print("SAVING AND LOADING MODELS")
print("=" * 70)

import joblib

# Save the model
joblib.dump(best_pipeline, 'loan_approval_model.pkl')
print("✓ Model saved to 'loan_approval_model.pkl'")

# Load the model
loaded_model = joblib.load('loan_approval_model.pkl')
print("✓ Model loaded from file")

# Verify it works
test_score = loaded_model.score(X_test, y_test)
print(f"✓ Loaded model test accuracy: {test_score:.4f}")


# SUMMARY: THE ULTIMATE TEMPLATE

print("\n" + "=" * 70)
print("SUMMARY: THE ULTIMATE TEMPLATE")
print("=" * 70)

template = """
┌─────────────────────────────────────────────────────────────────────────┐
│              THE COMPLETE SCIKIT-LEARN TEMPLATE                         │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  import pandas as pd                                                    │
│  import numpy as np                                                     │
│  from sklearn.model_selection import train_test_split, GridSearchCV    │
│  from sklearn.pipeline import Pipeline                                  │
│  from sklearn.compose import ColumnTransformer                          │
│  from sklearn.preprocessing import StandardScaler, OneHotEncoder        │
│  from sklearn.impute import SimpleImputer                              │
│  from sklearn.ensemble import RandomForestClassifier                    │
│  import joblib                                                          │
│                                                                         │
│  # 1. Load data                                                         │
│  df = pd.read_csv('data.csv')                                          │
│  X = df.drop('target', axis=1)                                         │
│  y = df['target']                                                       │
│                                                                         │
│  # 2. Split data                                                        │
│  X_train, X_test, y_train, y_test = train_test_split(                  │
│      X, y, test_size=0.2, random_state=42                              │
│  )                                                                      │
│                                                                         │
│  # 3. Define column types                                               │
│  num_features = ['col1', 'col2']                                       │
│  cat_features = ['col3', 'col4']                                       │
│                                                                         │
│  # 4. Build pipeline                                                    │
│  pipeline = Pipeline([                                                  │
│      ('preprocessor', ColumnTransformer([                               │
│          ('num', Pipeline([                                             │
│              ('imputer', SimpleImputer(strategy='median')),            │
│              ('scaler', StandardScaler())                              │
│          ]), num_features),                                             │
│          ('cat', Pipeline([                                             │
│              ('imputer', SimpleImputer(strategy='most_frequent')),     │
│              ('encoder', OneHotEncoder(handle_unknown='ignore'))       │
│          ]), cat_features)                                              │
│      ])),                                                               │
│      ('classifier', RandomForestClassifier())                          │
│  ])                                                                     │
│                                                                         │
│  # 5. Hyperparameter tuning                                             │
│  param_grid = {'classifier__n_estimators': [100, 200]}                 │
│  grid_search = GridSearchCV(pipeline, param_grid, cv=5, n_jobs=-1)     │
│  grid_search.fit(X_train, y_train)                                     │
│                                                                         │
│  # 6. Evaluate                                                          │
│  print(f"Test score: {grid_search.score(X_test, y_test)}")             │
│                                                                         │
│  # 7. Save for production                                               │
│  joblib.dump(grid_search.best_estimator_, 'model.pkl')                 │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
"""
print(template)

print("\nPart 5 Complete! Run Part 6 for 'Where Scikit-Learn Ends'.")
