import shutil

path = "backend/app.py"
shutil.copy(path, path + ".backup")

with open(path, "r", encoding="utf-8") as f:
    content = f.read()

old = 'CLIENT_SECRETS_FILE = os.path.join(os.path.dirname(__file__), "client_secret.json")'

if old not in content:
    print("找不到預期的舊程式碼，沒有做任何修改，請把這行結果貼給Claude看")
else:
    new = '''def _resolve_secret_path(filename: str) -> str:
    render_secret_path = os.path.join("/etc/secrets", filename)
    local_path = os.path.join(os.path.dirname(__file__), filename)
    if os.path.exists(render_secret_path):
        return render_secret_path
    return local_path


CLIENT_SECRETS_FILE = _resolve_secret_path("client_secret.json")'''
    content = content.replace(old, new, 1)

    debug_route = '''

@app.route("/api/debug-secret", methods=["GET"])
def _debug_secret():
    render_path = "/etc/secrets/client_secret.json"
    local_path = os.path.join(os.path.dirname(__file__), "client_secret.json")
    return jsonify({
        "render_secret_path": render_path,
        "render_secret_exists": os.path.exists(render_path),
        "local_path": local_path,
        "local_exists": os.path.exists(local_path),
        "resolved_CLIENT_SECRETS_FILE": CLIENT_SECRETS_FILE,
        "etc_secrets_dir_listing": os.listdir("/etc/secrets") if os.path.isdir("/etc/secrets") else "目錄不存在",
    })
'''
    marker = 'YT_READONLY_SCOPES = ["https://www.googleapis.com/auth/youtube"]'
    content = content.replace(marker, marker + debug_route, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print("修改完成！已備份舊版到 backend/app.py.backup")
