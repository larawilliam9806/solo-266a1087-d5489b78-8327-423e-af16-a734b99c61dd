"""本地开发入口：python run.py（生产由 gunicorn 启动）。"""

from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
