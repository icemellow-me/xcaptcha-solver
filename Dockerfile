FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY xcaptcha_solver.py .

ENV PORT=8899
ENV CF_API_TOKEN=""
ENV CF_ACCOUNT_ID=""
ENV SOLVER_API_KEY="1"
ENV TASK_TIMEOUT=120

EXPOSE 8899

CMD ["python", "xcaptcha_solver.py"]
