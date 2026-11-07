import json, os, pathlib, shutil, subprocess, tempfile, urllib.request, urllib.error, zipfile

API = "https://noyon-apk-master-build-server-kf12il.v2.appdeploy.ai"
AUD = "noyon-apk-master"
def request(url, method="GET", data=None, headers=None, timeout=60):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw.decode()) if raw else {}

def oidc_token():
    url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"] + "&audience=" + AUD
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())["value"]

def multipart(fields, file_field, filename, content, boundary):
    chunks = []
    for key, value in fields.items():
        chunks += [("--" + boundary + "\r\n").encode(), ('Content-Disposition: form-data; name="' + key + '"\r\n\r\n').encode(), str(value).encode(), b"\r\n"]
    chunks += [("--" + boundary + "\r\n").encode(), ('Content-Disposition: form-data; name="' + file_field + '"; filename="' + filename + '"\r\n').encode(), b"Content-Type: application/vnd.android.package-archive\r\n\r\n", content, b"\r\n", ("--" + boundary + "--\r\n").encode()]
    return b"".join(chunks)

def run(cmd, cwd, timeout=1800):
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=cwd, check=True, timeout=timeout)

def find_html(root):
    files = list(root.rglob("index.html"))
    if not files:
        files = list(root.rglob("*.html"))
    if not files:
        raise RuntimeError("ZIP/HTML project-এ index.html বা HTML ফাইল পাওয়া যায়নি")
    return files[0]

def make_webview_project(source, project):
    app = project / "app"
    assets = app / "src/main/assets"
    java = app / "src/main/java/com/noyon/apkmaster"
    assets.mkdir(parents=True, exist_ok=True)
    java.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, assets / "www", dirs_exist_ok=True)
    index = assets / "www/index.html"
    if not index.exists():
        first = find_html(source)
        shutil.copy2(first, index)
    (project / "settings.gradle").write_text("pluginManagement { repositories { google(); mavenCentral(); gradlePluginPortal() } }\ndependencyResolutionManagement { repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS); repositories { google(); mavenCentral() } }\nrootProject.name='NoyonGeneratedApp'\ninclude ':app'\n")
    (project / "build.gradle").write_text("plugins { id 'com.android.application' version '8.6.1' apply false }\n")
    (app / "build.gradle").write_text("plugins { id 'com.android.application' }\nandroid { namespace 'com.noyon.apkmaster'; compileSdk 35; defaultConfig { applicationId 'com.noyon.apkmaster'; minSdk 23; targetSdk 28; versionCode 1; versionName '1.0' } }\n")
    manifest = app / "src/main/AndroidManifest.xml"
    manifest.write_text("""<manifest xmlns:android="http://schemas.android.com/apk/res/android"><uses-permission android:name="android.permission.INTERNET"/><application android:theme="@android:style/Theme.Material.Light.NoActionBar" android:label="NOYON APP" android:usesCleartextTraffic="true"><activity android:name=".MainActivity" android:exported="true"><intent-filter><action android:name="android.intent.action.MAIN"/><category android:name="android.intent.category.LAUNCHER"/></intent-filter></activity></application></manifest>""")
    (java / "MainActivity.java").write_text("""package com.noyon.apkmaster; import android.app.Activity; import android.os.Bundle; import android.webkit.WebView; import android.webkit.WebViewClient; import android.webkit.WebSettings; public class MainActivity extends Activity { public void onCreate(Bundle b){ super.onCreate(b); WebView w=new WebView(this); WebSettings s=w.getSettings(); s.setJavaScriptEnabled(true); s.setDomStorageEnabled(true); w.setWebViewClient(new WebViewClient()); setContentView(w); w.loadUrl("file:///android_asset/www/index.html"); } }""")

def main():
    token = oidc_token()
    auth = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    claimed = request(API + "/api/build/claim", headers=auth)
    job = claimed.get("job")
    if not job:
        print("No queued APK jobs.")
        return
    job_id = str(job["id"])
    try:
        config = request(API + "/api/build/telegram-config", headers=auth)
        bot_token = config["telegram_bot_token"]
        with tempfile.TemporaryDirectory(prefix="noyon-apk-") as td:
            root = pathlib.Path(td)
            download = root / "project"
            req = urllib.request.Request(job["project_url"])
            with urllib.request.urlopen(req, timeout=120) as response, open(download, "wb") as out:
                shutil.copyfileobj(response, out)
            source = root / "source"
            source.mkdir()
            name = str(job.get("project_file_name") or "project.zip").lower()
            if name.endswith(".zip"):
                with zipfile.ZipFile(download) as z:
                    z.extractall(source)
            elif name.endswith((".html", ".htm")):
                shutil.copy2(download, source / "index.html")
            else:
                raise RuntimeError("Unsupported project file type")
            gradle_root = None
            for candidate in [source] + [p for p in source.iterdir() if p.is_dir()]:
                if (candidate / "settings.gradle").exists() or (candidate / "settings.gradle.kts").exists():
                    gradle_root = candidate
                    break
            if gradle_root is None:
                if not list(source.rglob("*.html")):
                    raise RuntimeError("ZIP-এ Android Gradle project বা HTML website নেই")
                gradle_root = root / "generated"
                gradle_root.mkdir()
                make_webview_project(source, gradle_root)
            wrapper = gradle_root / "gradlew"
            if wrapper.exists():
                wrapper.chmod(0o755)
                run(["./gradlew", "--no-daemon", "assembleDebug"], gradle_root)
            else:
                run(["gradle", "--no-daemon", "assembleDebug"], gradle_root)
            apks = list(gradle_root.rglob("*.apk"))
            if not apks:
                raise RuntimeError("Gradle build শেষ হলেও APK ফাইল পাওয়া যায়নি")
            apk = next((p for p in apks if p.name == "app-debug.apk"), apks[0])
            with open(apk, "rb") as f:
                content = f.read()
            boundary = "----NoyonAPKMasterBoundary"
            body = multipart({"chat_id": str(job["telegram_user_id"]), "caption": "✅ " + str(job.get("app_name") or "Your App") + " APK তৈরি হয়েছে!"}, "document", str(job.get("app_name") or "app").replace("/", "_") + ".apk", content, boundary)
            req = urllib.request.Request("https://api.telegram.org/bot" + bot_token + "/sendDocument", data=body, headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
            with urllib.request.urlopen(req, timeout=180) as response:
                telegram_result = json.loads(response.read().decode())
            if not telegram_result.get("ok"):
                raise RuntimeError("Telegram-এ APK পাঠানো যায়নি")
            request(API + "/api/build/complete", method="POST", data=json.dumps({"job_id": job_id}).encode(), headers={**auth, "Content-Type": "application/json"})
            print("Build completed and APK delivered to Telegram.")
    except Exception as exc:
        message = str(exc)[:450]
        try:
            request(API + "/api/build/fail", method="POST", data=json.dumps({"job_id": job_id, "error": message}).encode(), headers={**auth, "Content-Type": "application/json"})
        except Exception:
            pass
        raise

if __name__ == "__main__":
    main()
