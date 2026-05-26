import os

from dotenv import load_dotenv
from flask import Flask

app = Flask(__name__)

load_dotenv()
dbHost = str(os.getenv('DB_HOST'))
dbPort = str(os.getenv('DB_PORT'))
dbName = str(os.getenv('DB_NAME'))
dbUser = str(os.getenv('DB_USER'))
dbPassword = str(os.getenv('DB_PASS'))

@app.route('/')
def hello_world():  # put application's code here
    return 'Hello World!'


if __name__ == '__main__':
    app.run()
