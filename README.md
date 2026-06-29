# 🚀 DevOps Capstone Project

## Quick Demo

Start Docker Desktop first, then open Ubuntu/WSL and run:

```bash
cd ~/devops-labs/capstone
sh start.sh
```

That starts the Kind Kubernetes cluster, deploys the Flask app and Redis, starts Prometheus and Grafana, reloads the provisioned dashboards, and prints the app/monitoring URLs.

## Overview

Hi this is Bharath Kumar, This project demonstrates an end-to-end DevOps workflow by deploying a Python Flask application with Redis, containerizing it using Docker, orchestrating it with Kubernetes (Kind), monitoring it using Prometheus and Grafana, and automating validation using GitHub Actions.

The goal of this project is to simulate a production-ready DevOps environment while following Infrastructure as Code (IaC) and CI/CD best practices.

---

## Features

* Python Flask REST API
* Redis integration
* Docker containerization
* Docker Compose support
* Kubernetes deployment using Kind
* Prometheus metrics collection
* Grafana dashboards
* GitHub Actions CI pipeline
* Health check endpoint
* Metrics endpoint for monitoring

---

## Tech Stack

| Technology        | Purpose                     |
| ----------------- | --------------------------- |
| Python            | Backend Application         |
| Flask             | REST API                    |
| Redis             | Data Store                  |
| Docker            | Containerization            |
| Docker Compose    | Local multi-container setup |
| Kubernetes (Kind) | Container Orchestration     |
| Prometheus        | Metrics Collection          |
| Grafana           | Monitoring Dashboard        |
| GitHub Actions    | Continuous Integration      |
| Git               | Version Control             |
CI demo run: Thu Jun 25 23:07:45 UTC 2026
