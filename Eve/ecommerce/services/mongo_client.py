from pymongo import MongoClient
from django.conf import settings

client = MongoClient(settings.MONGODB["HOST"])
mongo_db = client[settings.MONGODB["DB_NAME"]]

products_collection = mongo_db["products_cache"]
usage_logs_collection = mongo_db["usage_logs"]