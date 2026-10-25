FROM python:3.12-slim
WORKDIR /tools
RUN pip install --no-cache-dir ldap3==2.9.1 tzdata==2025.2 PyMySQL==1.1.1 cryptography==43.0.3
COPY lab_activity.py .
ENV PYTHONUNBUFFERED=1
CMD ["python", "lab_activity.py"]
