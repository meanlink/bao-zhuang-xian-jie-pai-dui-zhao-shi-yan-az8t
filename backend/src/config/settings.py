import os
DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///./local.db')
REDIS_URL = os.getenv('REDIS_URL', 'redis://redis:6379/0')
JWT_SECRET = os.environ.get('JWT_SECRET', 'local-development-change-before-deploy')
ARTIFACT_DIR = os.getenv('ARTIFACT_DIR', '/artifacts')
# 联合试验（联邦汇总）中心的 SQLite 库；该子系统仅用标准库，可独立于主库运行
FEDERATION_DB = os.getenv('FEDERATION_DB', os.path.join(ARTIFACT_DIR, 'federation-center.db'))
