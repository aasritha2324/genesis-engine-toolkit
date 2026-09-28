# Implementation roadmap
- [x] Provision Cloud authentication and tenant-scoped advertiser schema.
- [x] Implement accounts, workspace creation, campaigns, ads, and analytics dashboard.
- [x] Implement authenticated simulator and key-verified click ingestion with transactional deduplication.
- [x] Add basic fraud detection and public health check.
- [ ] Deploy and measure the separate Docker/Kafka/Redis/Nginx/Prometheus/Grafana architecture. Blocked: this hosted app preview does not run a multi-container stack.
- [ ] Run comparative load and failure tests against a deployed multi-container stack. Blocked: no Docker deployment available in this hosted preview.
