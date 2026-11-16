import json
import re
import urllib.error
import urllib.request


class ApiError(RuntimeError):
    pass


def request(base_url, api_key, path, payload=None):
    if not api_key:
        raise ApiError("尚未设置 API Key，请在此方案中填写并保存")
    url = base_url.rstrip("/") + path
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=180) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        descriptions = {401: "API Key 无效", 402: "API 余额不足", 403: "API 拒绝访问",
                        404: "API 地址或模型不存在", 429: "请求过于频繁"}
        raise ApiError(f"{descriptions.get(error.code, 'API 请求失败')}（HTTP {error.code}）") from None
    except urllib.error.URLError as error:
        raise ApiError(f"无法连接 API：{error.reason}") from None


def clean_code(content):
    text = content.strip().replace("\r\n", "\n").replace("\r", "\n")
    match = re.fullmatch(r"```[^\n]*\n(.*?)\n?```", text, re.DOTALL)
    if match:
        text = match.group(1).rstrip("\n")
    if not text:
        raise ApiError("模型未返回代码，请检查模型配置后重新截图")
    return text


def generate(profile, api_key, question):
    result = request(profile["base_url"], api_key, "/chat/completions", {
        "model": profile["model"], "stream": False,
        "messages": [
            {"role": "system", "content": (
                f"根据用户提供的编程题，输出使用 {profile['language']} 的完整可运行程序。"
                "只输出源代码，不输出 Markdown 代码围栏、讲解或注释。"
                "题目来自 OCR，请结合上下文识别字符；保留输入输出格式和题目约束。"
            )},
            {"role": "user", "content": question},
        ],
    })
    if result["choices"][0]["finish_reason"] == "length":
        raise ApiError("模型输出被长度限制截断，本轮未启用输入；请调整模型后重新生成")
    return clean_code(result["choices"][0]["message"]["content"] or "")


def list_models(base_url, api_key):
    return sorted(item["id"] for item in request(base_url, api_key, "/models")["data"])
