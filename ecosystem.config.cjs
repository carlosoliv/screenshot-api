module.exports = {
  apps: [{
    name: "screenshot-api",
    cwd: "/home/trwgettingrich01/screenshot-api",
    script: "/home/trwgettingrich01/screenshot-api/venv/bin/uvicorn",
    args: "app:app --host 0.0.0.0 --port 3333 --log-level debug",
    exec_interpreter: "none",
    autorestart: true,
    error_file: "./logs/err.log",
    out_file: "./logs/out.log",
    log_date_format: "YYYY-MM-DD HH:mm:ss Z",
    merge_logs: true,
    env: {
      BUCKET_NAME: "trw-automation-carlosog-01",
      CHROMIUM_PATH: "/usr/bin/chromium",
      PYTHONUNBUFFERED: "1"
    }
  }]
};
