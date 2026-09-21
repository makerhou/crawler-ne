安装依赖
通过 HTTP 请求 在 python 中调用各种云开发能力

pip install requests python-dotenv
2
初始化配置
新增如下代码到您的 Python 项目

import os
import requests
from dotenv import load_dotenv

load_dotenv()

class CloudBaseClient:
	def __init__(self):
		self.env_id = os.getenv("CLOUDBASE_ENV_ID")
		self.access_token = os.getenv("CLOUDBASE_ACCESS_TOKEN")
		self.base_url = f"https://{self.env_id}.api.tcloudbasegateway.com"
		self.headers = {
			"Content-Type": "application/json",
			"Accept": "application/json",
			"Authorization": f"Bearer {self.access_token}"
		}

	def request(self, method, path, **kwargs):
		"""
		统一的HTTP请求方法

		Args:
			method: 请求方法 (GET, POST, PUT, PATCH, DELETE)
			path: API路径 (如 /v1/rdb/rest/table_name)
			**kwargs: 其他请求参数 (json, params, headers等)

		Returns:
			响应数据或None
		"""
		url = f"{self.base_url}{path}"
		headers = self.headers.copy()

		# 允许自定义headers
		if "headers" in kwargs:
			headers.update(kwargs.pop("headers"))

		try:
			response = requests.request(method, url, headers=headers, **kwargs)
			response.raise_for_status()

			# 如果响应为空，返回True表示成功
			if not response.content:
				return True

			return response.json()
		except requests.exceptions.RequestException as e:
			print(f"请求失败: {e}")
			return None

cloudbase = CloudBaseClient()

## 查询数据
```python
from cloudbase_client import cloudbase

def get_mysql_data(table_name):
	"""查询 MySQL 数据库数据"""
	data = cloudbase.request("GET", f"/v1/rdb/rest/{table_name}?limit=10")

	if data:
		print("查询成功:", data)
	return data or []

# 使用示例
if __name__ == "__main__":
	result = get_mysql_data("<YOUR_TABLE_NAME>")
```

## 插入数据
```python
from cloudbase_client import cloudbase

def add_mysql_data(table_name, data):
	"""新增 MySQL 数据库数据"""
	result = cloudbase.request("POST", f"/v1/rdb/rest/{table_name}", json=data)

	if result:
		print("新增成功:", result)
	return result

# 使用示例
if __name__ == "__main__":
	result = add_mysql_data("<YOUR_TABLE_NAME>", {"title": "示例标题"})
```

## 修改数据
```python
from cloudbase_client import cloudbase

def update_mysql_data(table_name, data_id, data):
	"""更新 MySQL 数据库数据"""
	result = cloudbase.request("PATCH", f"/v1/rdb/rest/{table_name}?id=eq.{data_id}", json=data)

	if result:
		print("更新成功:", result)
	return result

# 使用示例
if __name__ == "__main__":
	result = update_mysql_data("<YOUR_TABLE_NAME>", "<数据id>", {"title": "新标题"})
```

## 删除数据
```python
from cloudbase_client import cloudbase

def delete_mysql_data(table_name, data_id):
	"""删除 MySQL 数据库数据"""
	result = cloudbase.request("DELETE", f"/v1/rdb/rest/{table_name}?id=eq.{data_id}")

	if result:
		print("删除成功")
		return True
	return False

# 使用示例
if __name__ == "__main__":
	result = delete_mysql_data("<YOUR_TABLE_NAME>", "<数据id>")
```