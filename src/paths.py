"""项目运行时目录。"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
DATA_DIR = PROJECT_ROOT / "data"
TEMP_DIR = PROJECT_ROOT / "tmp"


def ensure_temp_dir() -> Path:
    """创建并返回项目临时文件目录。"""
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    return TEMP_DIR


def ensure_data_dir() -> Path:
    """创建并返回项目持久化数据目录。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR
