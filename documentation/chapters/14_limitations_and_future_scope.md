# CHAPTER 14: LIMITATIONS AND FUTURE ENHANCEMENTS

---

## 14.1 Current Technical Limitations

While GW Smart Analytics & ShopPulse represents a robust and feature-rich business intelligence solution, technical boundaries exist within the current implementation:

1. **Synchronous Upload Processing**: Large tabular datasets (> 20 MB) undergo synchronous in-memory parsing. In future releases, background task queues (e.g., Celery with Redis) should be introduced for asynchronous dataset processing.
2. **Single-Node File Storage**: Dataset uploads are stored on the local server filesystem (`UPLOAD_DIR`). While protected via path sandboxing, multi-instance horizontal scaling will require distributed cloud object storage (e.g., Amazon S3, Google Cloud Storage).
3. **Rule-Based Assistant Dependency**: While mathematically grounded, the current assistant queries rely on deterministic SQL mapping rather than full neural semantic embeddings.

---

## 14.2 Planned Enhancements & Roadmap

### 14.2.1 Advanced AI & Neural RAG Integration
- Implement the approved [AI Architecture Blueprint](file:///C:/Users/whari/.gemini/antigravity/brain/cf94f69f-213d-402a-adcd-6a943bba7c6b/ai_assistant_architecture_blueprint.md) incorporating OpenAI / Google Gemini LLM endpoints.
- Deploy vector embeddings (using `text-embedding-3-small`) to enable semantic search across user documentation, knowledge bases, and retail catalogs.
- Incorporate browser-native Web Speech STT and TTS voice synthesis for multilingual voice-driven inventory queries.

### 14.2.2 Predictive Machine Learning Models
- **Demand Forecasting**: Expand the existing Scikit-learn Random Forest regression model in [`hh.py`](file:///f:/ganesh/smart/42435/hh.py) to forecast next-month inventory demand based on historical seasonality.
- **Customer Churn Prediction**: Implement logistic regression models predicting which registered buyers have a high probability of lapsing based on purchase recency.

### 14.2.3 Mobile Progressive Web App (PWA)
- Implement Service Workers and a Web App Manifest (`manifest.json`) enabling offline POS cashier transactions with background sync when network connectivity is restored.
- Native barcode scanner hardware integration via the browser MediaDevices camera API.

### 14.2.4 Direct Cashfree Automated Webhooks
- Finalize the automated server-to-server webhook callback listener (`cashfree_pg`) to activate subscriptions automatically without requiring manual administrator UTR approval.
