"""Diagnose what's blocking gee-dataset-intelligence publish."""
import os
import subprocess
import shutil

SKILL = r"Z:\Mywork\自媒体\公众号\我的产品推文\gee-dataset-intelligence"
CLAWHUB = r"C:\Program Files\nodejs\clawhub.cmd"
EXCLUDE = {"_geoskill_core", "__pycache__", ".pytest_cache", ".git", ".claude-plugin", ".clawhub", ".tmp", "assets.tmp"}

# 移走 assets
assets = os.path.join(SKILL, "assets")
assets_tmp = os.path.join(SKILL, "assets.tmp")
moved_assets = False
if os.path.isdir(assets):
    if os.path.isdir(assets_tmp):
        shutil.rmtree(assets_tmp)
    os.rename(assets, assets_tmp)
    moved_assets = True

# 列所有 publish 会包括的文件
print("=== Files that will be uploaded ===")
total = 0
for root, dirs, files in os.walk(SKILL):
    parts = set(os.path.relpath(root, SKILL).split(os.sep))
    if parts & EXCLUDE:
        continue
    for f in files:
        p = os.path.join(root, f)
        try:
            sz = os.path.getsize(p)
            total += sz
            print(f"  {sz:>10,}  {os.path.relpath(p, SKILL)}")
        except OSError:
            pass
print(f"\nTOTAL: {total:,} bytes ({total/1024:.1f} KB / {total/1024/1024:.2f} MB)")

# 实际 publish
print("\n=== Attempting publish ===")
env = os.environ.copy()
env["CLAWHUB_TOKEN"] = "clh_Rv9Avpk7TWH6fSRmheFYSoKSoD2DVWgpAhvqV0PAwks"
env["CLAWHUB_API"] = "https://clawhub.ai/api"
proc = subprocess.run(
    [CLAWHUB, "publish", SKILL, "--changelog", "Phase 7.5 SKILL.md add Credentials section"],
    capture_output=True, text=True, env=env, timeout=120,
)
print(f"  exit: {proc.returncode}")
if proc.stdout.strip(): print(f"  stdout: {proc.stdout[:500]}")
if proc.stderr.strip(): print(f"  stderr: {proc.stderr[:500]}")

# 恢复
if moved_assets and os.path.isdir(assets_tmp):
    os.rename(assets_tmp, assets)
