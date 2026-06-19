"""
Scikit-Learn: Zero to Hero
==========================
Part 6: Where Scikit-Learn Ends and Deep Learning Begins

Understanding the boundaries and when to move beyond scikit-learn.
"""

print("=" * 70)
print("SCIKIT-LEARN: ZERO TO HERO")
print("Part 6: Where Scikit-Learn Ends and Deep Learning Begins")
print("=" * 70)


# ============================================================================
# WHAT SCIKIT-LEARN IS GOOD FOR
# ============================================================================

print("\n" + "=" * 70)
print("WHAT SCIKIT-LEARN IS GOOD FOR")
print("=" * 70)

sklearn_strengths = """
┌─────────────────────────────────────────────────────────────────────────┐
│                  SCIKIT-LEARN SWEET SPOT                                │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  DATA TYPES:                                                            │
│  ✓ Tabular data (spreadsheets, databases, CSVs)                        │
│  ✓ Structured numerical features                                        │
│  ✓ Categorical data                                                     │
│                                                                         │
│  DATASET SIZE:                                                          │
│  ✓ Small to medium (up to ~1 million rows typically)                   │
│  ✓ Fits in memory                                                       │
│                                                                         │
│  TASKS:                                                                 │
│  ✓ Classification (binary and multi-class)                             │
│  ✓ Regression                                                          │
│  ✓ Clustering                                                          │
│  ✓ Dimensionality reduction                                            │
│  ✓ Feature selection                                                   │
│  ✓ Model evaluation                                                    │
│                                                                         │
│  COMMON USE CASES:                                                      │
│  ✓ Customer churn prediction                                           │
│  ✓ Fraud detection                                                     │
│  ✓ Price prediction                                                    │
│  ✓ Medical diagnosis (from structured data)                            │
│  ✓ Recommendation systems (basic)                                      │
│  ✓ Anomaly detection                                                   │
│  ✓ Customer segmentation                                               │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
"""
print(sklearn_strengths)


# ============================================================================
# WHAT SCIKIT-LEARN CANNOT DO
# ============================================================================

print("\n" + "=" * 70)
print("WHAT SCIKIT-LEARN CANNOT DO")
print("=" * 70)

sklearn_limits = """
┌─────────────────────────────────────────────────────────────────────────┐
│                  SCIKIT-LEARN LIMITATIONS                               │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ✗ IMAGES                                                               │
│    - Image classification (cat vs dog)                                  │
│    - Object detection                                                   │
│    - Face recognition                                                   │
│    - Medical imaging                                                    │
│    → Use: PyTorch/TensorFlow with CNNs (Convolutional Neural Networks) │
│                                                                         │
│  ✗ TEXT (advanced)                                                      │
│    - Sentiment analysis at scale                                        │
│    - Text generation                                                    │
│    - Machine translation                                                │
│    - Question answering                                                 │
│    → Use: Transformers (BERT, GPT), HuggingFace                        │
│                                                                         │
│  ✗ AUDIO/SPEECH                                                         │
│    - Speech recognition                                                 │
│    - Music generation                                                   │
│    - Voice cloning                                                      │
│    → Use: PyTorch/TensorFlow with specialized architectures            │
│                                                                         │
│  ✗ VIDEO                                                                │
│    - Action recognition                                                 │
│    - Video generation                                                   │
│    → Use: 3D CNNs, Video Transformers                                  │
│                                                                         │
│  ✗ SEQUENCES (complex)                                                  │
│    - Time series forecasting (advanced)                                 │
│    - Language modeling                                                  │
│    → Use: RNNs, LSTMs, Transformers                                    │
│                                                                         │
│  ✗ GPU ACCELERATION                                                     │
│    - Scikit-learn is CPU-only                                          │
│    → Use: PyTorch/TensorFlow for GPU training                          │
│                                                                         │
│  ✗ VERY LARGE DATASETS                                                  │
│    - Billions of rows                                                   │
│    → Use: Spark MLlib, or batch training with deep learning            │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
"""
print(sklearn_limits)


# ============================================================================
# THE DEEP LEARNING LANDSCAPE
# ============================================================================

print("\n" + "=" * 70)
print("THE DEEP LEARNING LANDSCAPE")
print("=" * 70)

deep_learning = """
DEEP LEARNING = Neural Networks with many layers

Key frameworks:
┌─────────────────────────────────────────────────────────────────────────┐
│                                                                         │
│  PYTORCH (by Meta/Facebook)                                            │
│    - More pythonic, flexible                                           │
│    - Preferred for research                                            │
│    - Easier debugging                                                  │
│    - Growing fastest                                                    │
│                                                                         │
│  TENSORFLOW (by Google)                                                 │
│    - Production-focused                                                │
│    - TensorBoard for visualization                                     │
│    - TensorFlow Lite for mobile                                        │
│    - Keras as high-level API                                           │
│                                                                         │
│  HUGGING FACE                                                           │
│    - Pre-trained transformer models                                    │
│    - BERT, GPT, etc.                                                   │
│    - Best for NLP tasks                                                │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘

When to use deep learning:
- Unstructured data (images, text, audio)
- You have LOTS of data (deep learning is data-hungry)
- You have GPU resources
- State-of-the-art performance is critical
- The problem is too complex for traditional ML
"""
print(deep_learning)


# ============================================================================
# NEURAL NETWORK ARCHITECTURES
# ============================================================================

print("\n" + "=" * 70)
print("NEURAL NETWORK ARCHITECTURES (Preview)")
print("=" * 70)

architectures = """
┌─────────────────────────────────────────────────────────────────────────┐
│                  NEURAL NETWORK TYPES                                   │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  1. FEEDFORWARD (MLP - Multi-Layer Perceptron)                         │
│     ───────────────────────────────────                                 │
│     Input → Hidden → Hidden → Output                                    │
│     Use for: Tabular data (like sklearn, but can be more powerful)     │
│     Note: sklearn has MLPClassifier and MLPRegressor!                  │
│                                                                         │
│  2. CNN (Convolutional Neural Network)                                  │
│     ───────────────────────────────────                                 │
│     Specialized for grid-like data (images)                            │
│     Uses convolution operations to detect patterns                     │
│     Use for: Image classification, object detection                    │
│                                                                         │
│  3. RNN (Recurrent Neural Network)                                      │
│     ───────────────────────────────────                                 │
│     Has memory, processes sequences                                     │
│     Variants: LSTM, GRU                                                │
│     Use for: Time series, text (older approach)                        │
│                                                                         │
│  4. TRANSFORMER                                                         │
│     ───────────────────────────────────                                 │
│     Attention mechanism, parallel processing                           │
│     Revolution in NLP and beyond                                       │
│     Examples: BERT, GPT, Vision Transformer                            │
│     Use for: NLP, increasingly images and more                         │
│                                                                         │
│  5. GAN (Generative Adversarial Network)                               │
│     ───────────────────────────────────                                 │
│     Generator vs Discriminator                                         │
│     Use for: Image generation, style transfer                          │
│                                                                         │
│  6. AUTOENCODER                                                         │
│     ───────────────────────────────────                                 │
│     Compress and reconstruct                                           │
│     Use for: Anomaly detection, dimensionality reduction               │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
"""
print(architectures)


# ============================================================================
# SCIKIT-LEARN's NEURAL NETWORK (MLPClassifier)
# ============================================================================

print("\n" + "=" * 70)
print("SCIKIT-LEARN's NEURAL NETWORK")
print("=" * 70)

print("""
Scikit-learn DOES have a basic neural network: MLPClassifier / MLPRegressor
(MLP = Multi-Layer Perceptron)

Good for:
- Quick neural network experiments
- Tabular data
- When you don't need GPUs

Not good for:
- Images, text, audio
- Large datasets
- When you need GPU acceleration
""")

from sklearn.neural_network import MLPClassifier
from sklearn.datasets import load_breast_cancer
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler

# Load data
data = load_breast_cancer()
X_train, X_test, y_train, y_test = train_test_split(
    data.data, data.target, test_size=0.2, random_state=42
)

# Scale (important for neural networks!)
scaler = StandardScaler()
X_train_scaled = scaler.fit_transform(X_train)
X_test_scaled = scaler.transform(X_test)

# Create MLP
mlp = MLPClassifier(
    hidden_layer_sizes=(100, 50),  # Two hidden layers: 100 and 50 neurons
    activation='relu',             # ReLU activation (most common)
    max_iter=500,
    random_state=42
)

mlp.fit(X_train_scaled, y_train)

print(f"MLP Architecture: Input({X_train.shape[1]}) → 100 → 50 → Output(2)")
print(f"Training accuracy: {mlp.score(X_train_scaled, y_train):.4f}")
print(f"Test accuracy:     {mlp.score(X_test_scaled, y_test):.4f}")


# ============================================================================
# DECISION FLOWCHART: SKLEARN vs DEEP LEARNING
# ============================================================================

print("\n" + "=" * 70)
print("DECISION FLOWCHART: SKLEARN vs DEEP LEARNING")
print("=" * 70)

flowchart = """
                    ┌─────────────────┐
                    │  What's your    │
                    │  data type?     │
                    └────────┬────────┘
                             │
            ┌────────────────┼────────────────┐
            │                │                │
            ▼                ▼                ▼
       ┌─────────┐    ┌───────────┐    ┌───────────┐
       │ TABULAR │    │  IMAGES   │    │   TEXT    │
       │(CSV,DB) │    │           │    │           │
       └────┬────┘    └─────┬─────┘    └─────┬─────┘
            │               │                │
            ▼               │                │
    ┌───────────────┐       │                │
    │ Dataset size? │       │                │
    └───────┬───────┘       │                │
            │               │                │
      ┌─────┴─────┐         │                │
      │           │         │                │
      ▼           ▼         ▼                ▼
  ┌───────┐  ┌────────┐  ┌──────────┐  ┌──────────┐
  │ <100K │  │ >100K  │  │   CNN    │  │TRANSFORMER│
  │ rows  │  │ rows   │  │ PyTorch/ │  │ HuggingFace│
  └───┬───┘  └────┬───┘  │TensorFlow│  │ BERT, GPT │
      │           │      └──────────┘  └──────────┘
      ▼           ▼
  ┌───────┐  ┌────────┐
  │SKLEARN│  │SKLEARN │
  │ All   │  │  or    │
  │Models │  │Deep    │
  └───────┘  │Learning│
             └────────┘

SIMPLE RULE:
- Tabular data → Start with scikit-learn
- Images/Audio/Video → Deep learning (PyTorch/TensorFlow)
- Text → HuggingFace Transformers (or sklearn for simple tasks)
"""
print(flowchart)


# ============================================================================
# YOUR LEARNING PATH FORWARD
# ============================================================================

print("\n" + "=" * 70)
print("YOUR LEARNING PATH FORWARD")
print("=" * 70)

learning_path = """
┌─────────────────────────────────────────────────────────────────────────┐
│                    YOUR ML/DL LEARNING PATH                             │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  COMPLETED ✓                                                            │
│  ───────────                                                            │
│  ✓ ML fundamentals (math, gradient descent)                            │
│  ✓ Linear regression (from scratch + NumPy)                            │
│  ✓ Logistic regression (concepts)                                      │
│  ✓ Perceptrons                                                         │
│  ✓ Scikit-learn (this tutorial)                                        │
│                                                                         │
│  NEXT STEPS (choose based on interest):                                 │
│  ────────────────────────────────────                                   │
│                                                                         │
│  PATH A: Classical ML Depth                                             │
│    → More sklearn practice with real datasets                          │
│    → Kaggle competitions                                               │
│    → Feature engineering                                               │
│    → Ensemble methods deep dive                                        │
│                                                                         │
│  PATH B: Deep Learning                                                  │
│    → PyTorch basics                                                    │
│    → Neural network from scratch                                       │
│    → CNNs for images                                                   │
│    → Transformers for text                                             │
│                                                                         │
│  PATH C: AI + Cybersecurity (your interest!)                           │
│    → Anomaly detection (sklearn)                                       │
│    → Malware classification                                            │
│    → Adversarial ML (attacking/defending models)                       │
│    → LLM security                                                      │
│                                                                         │
│  PATH D: AI Engineering                                                 │
│    → LLM APIs (OpenAI, Anthropic)                                     │
│    → RAG systems                                                       │
│    → Deployment (FastAPI, Docker)                                      │
│    → MLOps                                                             │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘

RECOMMENDED: Do a few sklearn projects first, then branch out.
"""
print(learning_path)


# ============================================================================
# COMPLETE SKLEARN REFERENCE
# ============================================================================

print("\n" + "=" * 70)
print("COMPLETE SKLEARN REFERENCE")
print("=" * 70)

reference = """
┌─────────────────────────────────────────────────────────────────────────┐
│               SCIKIT-LEARN COMPLETE IMPORT REFERENCE                    │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  # DATA SPLITTING                                                       │
│  from sklearn.model_selection import train_test_split                  │
│  from sklearn.model_selection import cross_val_score, cross_validate   │
│  from sklearn.model_selection import KFold, StratifiedKFold            │
│  from sklearn.model_selection import GridSearchCV, RandomizedSearchCV  │
│                                                                         │
│  # PREPROCESSING                                                        │
│  from sklearn.preprocessing import StandardScaler, MinMaxScaler        │
│  from sklearn.preprocessing import LabelEncoder, OneHotEncoder         │
│  from sklearn.impute import SimpleImputer                              │
│                                                                         │
│  # PIPELINES                                                            │
│  from sklearn.pipeline import Pipeline                                  │
│  from sklearn.compose import ColumnTransformer                          │
│                                                                         │
│  # REGRESSION MODELS                                                    │
│  from sklearn.linear_model import LinearRegression, Ridge, Lasso       │
│  from sklearn.tree import DecisionTreeRegressor                        │
│  from sklearn.ensemble import RandomForestRegressor                    │
│  from sklearn.ensemble import GradientBoostingRegressor                │
│  from sklearn.svm import SVR                                           │
│                                                                         │
│  # CLASSIFICATION MODELS                                                │
│  from sklearn.linear_model import LogisticRegression, Perceptron       │
│  from sklearn.tree import DecisionTreeClassifier                       │
│  from sklearn.ensemble import RandomForestClassifier                   │
│  from sklearn.ensemble import GradientBoostingClassifier               │
│  from sklearn.svm import SVC                                           │
│  from sklearn.neighbors import KNeighborsClassifier                    │
│  from sklearn.naive_bayes import GaussianNB                            │
│  from sklearn.neural_network import MLPClassifier                      │
│                                                                         │
│  # CLUSTERING                                                           │
│  from sklearn.cluster import KMeans, DBSCAN, AgglomerativeClustering   │
│                                                                         │
│  # DIMENSIONALITY REDUCTION                                             │
│  from sklearn.decomposition import PCA                                  │
│  from sklearn.manifold import TSNE                                     │
│                                                                         │
│  # METRICS                                                              │
│  from sklearn.metrics import accuracy_score, precision_score           │
│  from sklearn.metrics import recall_score, f1_score                    │
│  from sklearn.metrics import confusion_matrix, classification_report   │
│  from sklearn.metrics import roc_auc_score, roc_curve                  │
│  from sklearn.metrics import mean_squared_error, r2_score              │
│                                                                         │
│  # DATASETS                                                             │
│  from sklearn.datasets import load_iris, load_breast_cancer            │
│  from sklearn.datasets import make_classification, make_regression     │
│                                                                         │
│  # SAVING MODELS                                                        │
│  import joblib                                                          │
│  joblib.dump(model, 'model.pkl')                                       │
│  model = joblib.load('model.pkl')                                      │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
"""
print(reference)


print("\n" + "=" * 70)
print("CONGRATULATIONS!")
print("=" * 70)

print("""
You now have a complete understanding of scikit-learn!

What you've learned:
1. The universal fit/predict/score API
2. All major algorithms (regression, classification, clustering)
3. Model evaluation and metrics
4. Cross-validation and hyperparameter tuning
5. Pipelines for clean, production-ready code
6. When to use sklearn vs deep learning

Next: Practice on real datasets!
- Kaggle.com has great beginner datasets
- UCI Machine Learning Repository
- Your own data!

Happy modeling! 🎉
""")
