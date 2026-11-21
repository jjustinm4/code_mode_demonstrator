FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py boot.py ./
ENV PYTHONUNBUFFERED=1
# boot.py is PID 1 (single process) so an OOM kill takes the whole container down.
CMD ["python", "boot.py"]
