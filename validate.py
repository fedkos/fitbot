"""Проверка данных перед рестартом: python validate.py"""
import json
import sys

import config
import program as P
import quick as Q


def main() -> int:
    try:
        programs = P.load_programs(config.PROGRAM_DIR)
    except P.ProgramError as e:
        print(f"ОШИБКА программы: {e}")
        return 1
    for p in P.ordered(programs):
        print(f"OK  {p['id']}: {p['title']} ({p['days']} дн., {len(p['exercises'])} упражнений)")
        missing = [e for e in p["exercises"] if not P.find_image(config.IMAGE_DIR, e, P.base_step(p, e))]
        if missing:
            print(f"    без картинки (кнопка не показывается): {', '.join(missing)}")
    try:
        q = Q.load(config.QUICK_FILE)
        print(f"OK  {config.QUICK_FILE.name}: разовые, {len(q['exercises'])} упражнений")
    except Q.QuickError as e:
        print(f"ОШИБКА разовых: {e}")
        return 1
    if config.DATA_FILE.exists():
        try:
            data = json.loads(config.DATA_FILE.read_text(encoding="utf-8"))
            print(f"OK  {config.DATA_FILE.name}: пользователей {len(data.get('users', {}))}")
        except Exception as e:  # бот такой файл отведёт в .broken, но лучше знать заранее
            print(f"ОШИБКА {config.DATA_FILE.name}: {e}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
