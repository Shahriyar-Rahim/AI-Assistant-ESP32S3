# Deployment entry points

The local container stack is defined in the repository-root [`../docker-compose.yml`](../docker-compose.yml); service build contexts and Dockerfiles are in `backend/` and `dashboard/`. Provider-specific setup and operational limitations are in [`../docs/deployment.md`](../docs/deployment.md).

Before deploying publicly, complete dashboard authentication, API authorization/rate limits, production database migrations/backups, secret encryption/rotation, and the remaining integration implementations listed in [`../IMPLEMENTATION_STATUS.md`](../IMPLEMENTATION_STATUS.md). The current baseline is not a production-ready public service.
