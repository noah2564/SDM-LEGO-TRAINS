FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY simulator.py ./

RUN useradd --create-home --uid 10002 simuser
USER simuser

ENTRYPOINT ["python", "simulator.py"]
