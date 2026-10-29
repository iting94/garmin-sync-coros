import os
import sys

CURRENT_DIR = os.path.split(os.path.abspath(__file__))[0]  # 当前目录
config_path = CURRENT_DIR.rsplit('/', 1)[0]  # 上三级目录
sys.path.append(config_path)

from config import DB_DIR, GARMIN_FIT_DIR
from garmin.garmin_client import GarminClient
from garmin.garmin_db import GarminDB
from coros.coros_client import CorosClient
from oss.ali_oss_client import AliOssClient
from oss.aws_oss_client import AwsOssClient
from utils.md5_utils import calculate_md5_file

# 只同步最新的 N 条（增量优先）。设为 0 或负数则恢复全量扫描。
LATEST_N = 5

SYNC_CONFIG = {
    'GARMIN_AUTH_DOMAIN': '',
    'GARMIN_EMAIL': '',
    'GARMIN_PASSWORD': '',
    'GARMIN_NEWEST_NUM': LATEST_N,
    "COROS_EMAIL": '',
    "COROS_PASSWORD": '',
}


def init(coros_db):
    ## 判断RQ数据库是否存在
    print(os.path.join(DB_DIR, coros_db.garmin_db_name))
    if not os.path.exists(os.path.join(DB_DIR, coros_db.garmin_db_name)):
        ## 初始化建表
        coros_db.initDB()
    if not os.path.exists(GARMIN_FIT_DIR):
        os.mkdir(GARMIN_FIT_DIR)


def upload_one(corosClient, file_path, un_sync_id):
    """上传单个 fit 文件到高驰，成功返回 True。"""
    client = None
    ## 中国区使用阿里云OSS (STS 凭证需登录 token 鉴权)
    if corosClient.regionId == 2:
        client = AliOssClient(access_token=corosClient.accessToken)
    elif corosClient.regionId == 1 or corosClient.regionId == 3:
        client = AwsOssClient()

    md5 = calculate_md5_file(file_path)
    client.multipart_upload(file_path, f"{corosClient.userId}/{md5}.zip")
    size = os.path.getsize(file_path)
    return corosClient.uploadActivity(
        f"fit_zip/{corosClient.userId}/{md5}.zip", md5, f"{un_sync_id}.zip", size
    )


if __name__ == "__main__":

    # 首先读取 面板变量 或者 github action 运行变量
    for k in SYNC_CONFIG:
        if os.getenv(k):
            v = os.getenv(k)
            SYNC_CONFIG[k] = v

    try:
        newest_num = int(SYNC_CONFIG["GARMIN_NEWEST_NUM"])
    except (TypeError, ValueError):
        newest_num = LATEST_N

    ## db 名称
    db_name = "garmin.db"
    ## 建立DB链接
    garmin_db = GarminDB(db_name)
    ## 初始化DB位置和下载文件位置
    init(garmin_db)

    GARMIN_EMAIL = SYNC_CONFIG["GARMIN_EMAIL"]
    GARMIN_PASSWORD = SYNC_CONFIG["GARMIN_PASSWORD"]
    GARMIN_AUTH_DOMAIN = SYNC_CONFIG["GARMIN_AUTH_DOMAIN"]

    garminClient = GarminClient(GARMIN_EMAIL, GARMIN_PASSWORD, GARMIN_AUTH_DOMAIN, newest_num)

    COROS_EMAIL = SYNC_CONFIG["COROS_EMAIL"]
    COROS_PASSWORD = SYNC_CONFIG["COROS_PASSWORD"]
    corosClient = CorosClient(COROS_EMAIL, COROS_PASSWORD)
    corosClient.login()

    # ---------------------------------------------------------------
    # 阶段一：只取最新 N 条（默认 5），先入库、先上传。
    # 不做全量翻页，因此几秒内就能拿到新数据。
    # ---------------------------------------------------------------
    if newest_num > 0:
        latest_activities = garminClient.getActivities(start=0, limit=newest_num)
    else:
        latest_activities = garminClient.getAllActivities()

    if not latest_activities:
        print("No activity returned by Garmin.")
        exit()

    print(f"Fetched {len(latest_activities)} latest activities from Garmin.")
    for activity in latest_activities:
        garmin_db.saveActivity(activity["activityId"])

    un_sync_id_list = garmin_db.getUnSyncActivity()
    if not un_sync_id_list:
        print("Nothing new to sync. All latest activities already in COROS.")
        exit()

    # 按佳明返回顺序（最新在前）逐个处理，保证最新的一条最先上传
    ordered = [a["activityId"] for a in latest_activities]
    un_sync_id_list = [i for i in ordered if i in set(un_sync_id_list)]
    print(f"Pending upload: {un_sync_id_list}")

    sync_failed = False
    for un_sync_id in un_sync_id_list:
        file_path = os.path.join(GARMIN_FIT_DIR, f"{un_sync_id}.zip")
        try:
            file = garminClient.downloadFitActivity(un_sync_id)
            with open(file_path, "wb") as fb:
                fb.write(file)
        except Exception as err:
            print(f"download failed {un_sync_id}: {err}")
            continue

        try:
            if upload_one(corosClient, file_path, un_sync_id):
                garmin_db.updateSyncStatus(un_sync_id)
                print(f"synced {un_sync_id}")
        except Exception as err:
            print(f"upload failed {un_sync_id}: {err}")
            garmin_db.updateExceptionSyncStatus(un_sync_id)
            sync_failed = True

    if sync_failed:
        sys.exit(1)
