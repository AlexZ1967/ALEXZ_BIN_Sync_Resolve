"""Synchronize a disk directory with a dedicated Resolve Media Pool bin."""

import ctypes
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath


EXTENSIONS = set("mov mp4 mxf mkv avi m4v mts m2ts webm braw r3d wav aif aiff mp3 flac m4a png jpg jpeg tif tiff exr dpx".split())
STILLS = set("png jpg jpeg tif tiff exr dpx".split())
RECOVERY = ".resolve-bin-sync-recovery"
ROOT = ""


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def component(name):
    require(name not in ("", ".", "..") and "/" not in name and
            not any(ord(ch) < 32 for ch in name) and not name.startswith("."),
            f"Имя не подходит для синхронизации: {name!r}")
    return name


def media(name):
    return PurePosixPath(name).suffix.lower()[1:] in EXTENSIONS


def identity(info):
    return f"{info.st_dev}:{info.st_ino}"


def is_still(path):
    return PurePosixPath(path).suffix.lower()[1:] in STILLS


def sequence_path(path):
    """Recognize Resolve paths that represent a range of numbered images."""
    if not is_still(path):
        return False
    stem = PurePosixPath(path).stem
    return bool(re.search(r"\[\d+\s*-\s*\d+\]|%0?\d*d|#{2,}", stem))


def join(parent, name):
    return f"{parent}/{name}" if parent else name


def parent_of(path):
    return path.rpartition("/")[0]


def under(path, folder):
    return path.startswith(folder + "/")


def stat_regular(path):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and path.resolve(strict=True) == path,
            f"Ожидался обычный файл без символических ссылок: {path}")
    return info


@dataclass(frozen=True)
class Entry:
    kind: str
    id: str
    source: str = ""
    stamp: tuple = ()
    obj: object = field(default=None, compare=False)


def disk_tree(root, root_id):
    """Read a complete snapshot of managed paths on one mounted filesystem."""
    result = {}
    root_info = root.lstat()
    require(stat.S_ISDIR(root_info.st_mode) and root.resolve(strict=True) == root and
            identity(root_info) == root_id, "Привязанная папка недоступна или была заменена.")

    def scan(folder, prefix):
        for path in sorted(folder.iterdir()):
            if path.name.startswith("."):
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                continue
            is_dir = stat.S_ISDIR(info.st_mode)
            if not is_dir and not (stat.S_ISREG(info.st_mode) and media(path.name)):
                continue
            require(info.st_dev == root_info.st_dev, f"Вложенная точка монтирования: {path}")
            rel = join(prefix, component(path.name))
            result[rel] = Entry("bin" if is_dir else "clip", identity(info),
                                stamp=() if is_dir else (info.st_size, info.st_mtime_ns))
            if is_dir:
                scan(path, rel)

    scan(root, ROOT)
    return result


def resolve_tree(pool, linked_id, name):
    """Find the linked bin and collect current clip references in the whole Media Pool."""
    master = pool.GetRootFolder()
    folders, clips = {}, {}
    linked = None

    def walk(folder, inside=False, prefix=ROOT):
        nonlocal linked
        folder_id = folder.GetUniqueId()
        require(folder_id and folder_id not in folders and not folder.GetIsFolderStale(),
                "Resolve вернул неполное или устаревшее дерево Media Pool.")
        folders[folder_id] = folder
        if folder_id == linked_id:
            linked = folder
            inside, prefix = True, ROOT
        children, items = folder.GetSubFolderList(), folder.GetClipList()
        require(children is not None and items is not None, "Не удалось прочитать Media Pool.")
        for item in items:
            item_id = item.GetUniqueId()
            require(item_id and item_id not in clips, "Resolve вернул повторяющийся ID клипа.")
            source = item.GetClipProperty("File Path") or ""
            clips[item_id] = (item, source, prefix if inside else None)
        for child in children:
            child_path = join(prefix, component(child.GetName())) if inside else ROOT
            walk(child, inside, child_path)

    walk(master)
    if not linked_id:
        require(all(folder.GetName() != name for folder in master.GetSubFolderList()),
                f"В корне Media Pool уже есть bin «{name}». Укажи отдельную директорию или переименуй существующий bin.")
        return master, {}, folders, clips
    require(linked is not None, "Корневой bin привязки отсутствует. Синхронизация остановлена.")
    result = {}

    def add(path, entry):
        require(path not in result, f"В Media Pool несколько элементов по пути {path}.")
        result[path] = entry

    def read(folder, prefix):
        for child in folder.GetSubFolderList():
            path = join(prefix, component(child.GetName()))
            add(path, Entry("bin", child.GetUniqueId(), obj=child))
            read(child, path)

    read(linked, ROOT)
    for uid, (item, source, prefix) in clips.items():
        if prefix is None or not source or not Path(source).is_absolute() or not media(Path(source).name) or sequence_path(source):
            continue
        add(join(prefix, component(Path(source).name)), Entry("clip", uid, source, obj=item))
    return linked, result, folders, clips


def make_plan(previous, disk, bins, folders, clips, root):
    """Merge each entry's parent and name against the last synchronized tree."""
    left, right = {}, {}
    used_disk, used_bins = set(), set()
    by_inode, by_bin = {}, {e.id: p for p, e in bins.items()}
    for path, entry in disk.items():
        by_inode.setdefault(entry.id, []).append(path)

    for key, old in previous.items():
        kind, disk_id, bin_id = old["kind"], old["disk"], old["bin"]
        matches = by_inode.get(disk_id, [])
        require(len(matches) <= 1, f"Несколько жёстких ссылок для {key}.")
        if matches:
            path = matches[0]
            require(disk[path].kind == kind and path not in used_disk, f"Изменилась идентичность {key}.")
            left[key] = path
            used_disk.add(path)
        elif key in disk and kind == "clip" and disk[key].kind == kind:
            # Монтажные приложения иногда сохраняют файл атомарной заменой.
            require(key not in used_disk and disk[key].id not in {v["disk"] for v in previous.values()},
                    f"Неоднозначная замена файла {key}.")
            left[key] = key
            used_disk.add(key)
        elif key in disk:
            raise RuntimeError(f"По прежнему пути появился другой объект: {key}.")
        if bin_id in by_bin:
            path = by_bin[bin_id]
            require(bins[path].kind == kind, f"Resolve изменил тип объекта {key}.")
            right[key] = path
            used_bins.add(path)
            if kind == "clip":
                expected = {str(root / key)}
                if key in left:
                    expected.add(str(root / left[key]))
                require(bins[path].source in expected, f"Клип вручную перелинкован: {path}.")
        else:
            require(bin_id not in folders and bin_id not in clips,
                    f"Объект перемещён за пределы привязки или перестал быть обычным медиа: {key}.")

    for path in sorted(set(disk) - used_disk):
        key = "+" + uuid.uuid4().hex
        left[key] = path
        if path in bins and path not in used_bins:
            require(disk[path].kind == bins[path].kind and
                    (disk[path].kind == "bin" or bins[path].source == str(root / path)),
                    f"Диск и Media Pool содержат разные объекты по пути {path}.")
            right[key] = path
            used_bins.add(path)
    for path in sorted(set(bins) - used_bins):
        right["+" + uuid.uuid4().hex] = path

    disk_keys = {path: key for key, path in left.items()}
    bin_keys = {path: key for key, path in right.items()}
    kinds, desired = {}, {}

    def position(path, keys):
        parent, _, name = path.rpartition("/")
        return (keys[parent] if parent else None, name)

    for key in dict.fromkeys([*previous, *left, *right]):
        lp, rp = left.get(key), right.get(key)
        kinds[key] = disk[lp].kind if lp is not None else bins[rp].kind if rp is not None else previous[key]["kind"]
        lpos = position(lp, disk_keys) if lp is not None else None
        rpos = position(rp, bin_keys) if rp is not None else None
        if key not in previous:
            if lpos is not None and rpos is not None:
                require(lpos == rpos, f"Разные родительские папки для {lp}.")
            desired[key] = lpos if lpos is not None else rpos
        else:
            old_parent, _, old_name = key.rpartition("/")
            before = (old_parent or None, old_name)
            if lpos == rpos or rpos == before:
                desired[key] = lpos
            elif lpos == before:
                desired[key] = rpos
            elif lpos is not None and rpos is not None:
                merged = []
                for original, a, b in zip(before, lpos, rpos):
                    require(a == b or a == original or b == original, f"Конфликт перемещений: {key}.")
                    merged.append(b if a == original else a)
                desired[key] = tuple(merged)
            else:
                raise RuntimeError(f"Удаление пересекается с перемещением: {key}.")

    targets, occupied = {}, set()

    def target(key, trail):
        if key not in targets:
            require(key not in trail and desired.get(key) is not None,
                    f"Перемещение образует цикл или оставляет объект в удаляемой папке: {left.get(key) or right.get(key)}.")
            parent, name = desired[key]
            path = join(target(parent, trail | {key}) if parent is not None else ROOT, name)
            require(path not in occupied, f"Два объекта претендуют на {path}.")
            occupied.add(path)
            targets[key] = path
        return targets[key]

    for key, position_value in desired.items():
        if position_value is not None:
            target(key, set())
    return left, right, kinds, targets


def schedule(current, targets, kinds, side):
    """Simulate operations before any writes and return an executable order."""
    paths = dict(current)
    ops = []
    settled = {ROOT}
    for kind in ("bin", "clip"):
        pending = [key for key in targets if kinds[key] == kind]
        while pending:
            progressed = False
            for key in pending[:]:
                dest = targets[key]
                old = paths.get(key)
                if parent_of(dest) not in settled:
                    continue
                if dest != old:
                    if dest in paths.values():
                        continue
                    if old is not None and kind == "bin" and under(dest, old):
                        continue
                    ops.append((side, "add" if old is None else "move", key, old, dest))
                    if old is not None and kind == "bin":
                        paths = {k: dest + p[len(old):] if under(p, old) else p for k, p in paths.items()}
                    paths[key] = dest
                settled.add(dest)
                pending.remove(key)
                progressed = True
            require(progressed, f"Заняты пути назначения ({side}): " +
                    ", ".join(targets[key] for key in pending))
    for key in sorted(set(paths) - set(targets), key=lambda k: paths[k].count("/"), reverse=True):
        ops.append((side, "remove", key, paths[key], None))
    return ops


def move_noreplace(source, destination):
    """Rename on Linux without ever replacing the destination."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, f"{os.strerror(error)}: {source} → {destination}")


def copy_noreplace(source, destination):
    """Publish a complete copy only if the source remained unchanged."""
    before = stat_regular(source)
    fd, temporary = tempfile.mkstemp(prefix=".bin-sync-", dir=destination.parent)
    os.close(fd)
    try:
        shutil.copy2(source, temporary)
        after = stat_regular(source)
        require((identity(before), before.st_size, before.st_mtime_ns) ==
                (identity(after), after.st_size, after.st_mtime_ns),
                f"Источник изменился во время копирования: {source}.")
        move_noreplace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def retarget(pool, item, destination):
    """Relink the existing Media Pool item and verify its new source."""
    old = Path(item.GetClipProperty("File Path"))
    if old == destination:
        return
    if old.name == destination.name:
        ok = pool.RelinkClips([item], str(destination.parent))
    else:
        ok = item.ReplaceClipPreserveSubClip(str(destination))
    require(ok and item.GetClipProperty("File Path") == str(destination),
            f"Resolve не перелинковал клип на {destination}.")


def import_file(pool, storage, folder, source):
    """Try supported import forms and locate the new item in the target bin."""
    path = str(source)
    current = pool.GetCurrentFolder()
    if not current or current.GetUniqueId() != folder.GetUniqueId():
        pool.SetCurrentFolder(folder)
        current = pool.GetCurrentFolder()
    require(current and current.GetUniqueId() == folder.GetUniqueId(),
            f"Resolve не открыл целевой bin для {path}.")

    def items():
        active = pool.GetCurrentFolder()
        require(active and active.GetUniqueId() == folder.GetUniqueId(),
                f"Resolve сменил целевой bin во время импорта {path}.")
        result = active.GetClipList()
        require(result is not None, f"Resolve не прочитал целевой bin для {path}.")
        if isinstance(result, dict):
            result = result.values()
        return {item.GetUniqueId(): item for item in result}

    before = items()
    require(not any(item.GetClipProperty("File Path") == path for item in before.values()),
            f"В целевом bin уже есть клип для {path}.")
    diagnostics = []
    plain = ("ImportMedia(пути)", lambda: pool.ImportMedia([path]))
    explicit = ("ImportMedia(FilePath)", lambda: pool.ImportMedia([{"FilePath": path}]))
    storage_import = ("Media Storage", lambda: storage.AddItemListToMediaPool([{"media": path}]))
    variants = (explicit, plain, storage_import) if is_still(path) else (plain, explicit, storage_import)
    for label, attempt in variants:
        returned = []
        try:
            response = attempt()
            if isinstance(response, dict):
                returned = list(response.values())
            elif isinstance(response, (list, tuple)):
                returned = list(response)
            count = len(returned)
            diagnostics.append(f"{label}: {type(response).__name__}, элементов {count}")
        except Exception as error:
            count = 0
            diagnostics.append(f"{label}: {error}")
        after = items()
        added = [item for uid, item in after.items() if uid not in before]
        if len(added) == 1 and before.keys() <= after.keys():
            actual = added[0].GetClipProperty("File Path") or ""
            if actual == path:
                return added[0], ""
            # Если Resolve сгруппировал кадры, убираем только элемент, возвращённый этим вызовом.
            if (is_still(path) and sequence_path(actual) and Path(actual).parent == source.parent
                    and count == 1 and hasattr(returned[0], "GetUniqueId")
                    and returned[0].GetUniqueId() == added[0].GetUniqueId()):
                require(pool.DeleteClips(added) and items().keys() == before.keys(),
                        f"Resolve не убрал последовательность, созданную при импорте {path}.")
                return None, f"Resolve сгруппировал кадры в {actual}. Включи Show Individual Frames."
            diagnostics.append(f"File Path нового клипа: {actual!r}")
        require(not added and not count and before.keys() <= after.keys(),
                f"Неожиданный результат импорта {path}. " + " | ".join(diagnostics))
    return None, " | ".join(diagnostics)


def timeline_references(project):
    """Read direct Media Pool references from project timelines."""
    used = set()
    for index in range(1, project.GetTimelineCount() + 1):
        timeline = project.GetTimelineByIndex(index)
        require(timeline, f"Не удалось прочитать таймлайн №{index}.")
        for track_type in ("video", "audio"):
            for track in range(1, timeline.GetTrackCount(track_type) + 1):
                items = timeline.GetItemListInTrack(track_type, track)
                require(items is not None, "Не удалось прочитать дорожку таймлайна.")
                for item in items:
                    clip = item.GetMediaPoolItem()
                    if clip:
                        used.add(clip.GetUniqueId())
    return used


def preflight(root, project, previous, disk, bins, clips, plan):
    """Reject unsafe deletes, shared sources, and unreadable inputs before writing."""
    left, right, kinds, targets = plan
    deleting = set(kinds) - set(targets)
    resolve_deletes = {bins[right[key]].id for key in deleting if key in right and kinds[key] == "clip"}
    if resolve_deletes:
        require(not (resolve_deletes & timeline_references(project)),
                "Один из удаляемых клипов используется на таймлайне. Сначала разберись с ним в Resolve.")
    managed = {entry.id for entry in bins.values() if entry.kind == "clip"}
    external_references = {str(Path(source).resolve()) for uid, (_, source, _) in clips.items()
                           if uid not in managed and source and Path(source).is_absolute()}

    for key, path in left.items():
        dest = targets.get(key)
        if kinds[key] == "clip" and dest != path:
            require(str(root / path) not in external_references,
                    f"На {path} ссылается клип вне привязки или неподдерживаемый клип.")
        if key in deleting and kinds[key] == "clip":
            old = previous[key]
            require(list(disk[path].stamp) == old["stamp"] and disk[path].id == old["disk"],
                    f"Клип удалён из bin, но файл изменён на диске: {path}.")
        if key in deleting and kinds[key] == "bin":
            for child in (root / path).iterdir():
                require(join(path, child.name) in disk,
                        f"В удаляемой папке есть файл или ссылка вне синхронизации: {child}.")
    for key, path in right.items():
        if key in deleting and kinds[key] == "bin":
            require(not any(uid not in managed and prefix is not None and
                            (prefix == path or under(prefix, path))
                            for uid, (_, _, prefix) in clips.items()),
                    f"Удаляемый bin содержит элементы вне синхронизации: {path}.")
        if key not in left and key in targets and kinds[key] == "clip":
            source = Path(bins[path].source)
            stat_regular(source)
            if root in source.parents:
                relative = source.relative_to(root).as_posix()
                existing = next((other for other, origin in left.items() if origin == relative), None)
                require(existing is not None and targets.get(existing) == relative,
                        f"Исходник копии одновременно перемещается или удаляется: {relative}.")


def write_json(path, data):
    """Replace a state file atomically."""
    fd, temp = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


class Session:
    """Keep the project binding, preview, and apply operation together."""

    def __init__(self, manager, project, storage):
        self.storage = storage
        self.manager = manager
        self.project = project
        self.pool = project.GetMediaPool()
        self.pool_id = self.pool.GetUniqueId()
        require(self.pool_id, "Resolve не сообщил ID Media Pool.")
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
        base = base / "alexz-resolve-bin-sync"
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.base = base
        self.project_key = hashlib.sha256(self.pool_id.encode()).hexdigest()
        self.lock = (base / (self.project_key + ".lock")).open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise RuntimeError("Окно Bin Sync для этого проекта уже открыто.") from None
        try:
            self.bindings = {}
            prefix = self.project_key
            for file in sorted(base.glob(prefix + "*.json")):
                slot = file.name.split(".pending.json")[0] if file.name.endswith(".pending.json") else file.stem
                if slot != prefix and not re.fullmatch(re.escape(prefix) + r"-[0-9a-f]{32}", slot):
                    continue
                if slot in self.bindings or (base / (slot + ".detached")).exists():
                    continue
                self.register(slot)
            self.master_id = self.pool.GetRootFolder().GetUniqueId()
            selected = self.pool.GetCurrentFolder()
            require(selected, "Resolve не сообщил выбранный bin.")
            self.selected_id = selected.GetUniqueId()
            self.selected_name = selected.GetName()
            parents = self.ancestors(selected)
            linked = self.bindings_by_bin()
            active = next((linked[folder_id] for folder_id in parents if folder_id in linked), None)
            self.select(active)
        except Exception:
            self.lock.close()
            raise

    def register(self, slot):
        """Read a binding and its journal, including the legacy project slot."""
        state = self.base / (slot + ".json")
        pending = self.base / (slot + ".pending.json")
        saved = json.loads(state.read_text(encoding="utf-8")) if state.exists() else None
        journal = json.loads(pending.read_text(encoding="utf-8")) if pending.exists() else None
        if saved:
            require(saved["version"] == 1, "Неизвестная версия файла привязки.")
        record = saved or journal
        if record:
            self.bindings[slot] = record
        return saved

    def select(self, slot):
        """Choose which binding the window controls."""
        self.preview = None
        self.active_slot = slot
        self.slot = slot or (self.project_key + "-" + uuid.uuid4().hex)
        self.state = self.base / (self.slot + ".json")
        self.pending = self.base / (self.slot + ".pending.json")
        self.binding = self.register(self.slot)
        self.attach_id = None if slot or self.selected_id == self.master_id else self.selected_id

    def ancestors(self, folder):
        """List bin IDs from the selected bin to the Media Pool root."""
        result = []
        while folder:
            uid = folder.GetUniqueId()
            require(uid and uid not in result, "Resolve вернул неверную иерархию bin.")
            result.append(uid)
            if uid == self.master_id:
                return result
            folder = folder.GetParentFolder()
        raise RuntimeError("Не удалось найти корень Media Pool для выбранного bin.")

    def bindings_by_bin(self):
        result = {}
        for slot, record in self.bindings.items():
            uid = record.get("bin")
            if uid:
                require(uid not in result, "Найдено несколько записей привязки для одного bin.")
                result[uid] = slot
        return result

    def check_scope(self, root, root_id, current_bin, folders, bins):
        """Reject overlapping disk trees or Media Pool trees."""
        current_ids = {entry.id for entry in bins.values()}
        for slot, record in self.bindings.items():
            if slot == self.slot:
                continue
            other = Path(record["root"])
            require(not (root == other or root in other.parents or other in root.parents or
                         root_id == record.get("root_id")),
                    f"Директория пересекается с другой привязкой: {other}.")
            other_bin = record.get("bin")
            require(not any(entry["bin"] in current_ids for entry in record.get("entries", {}).values()),
                    f"В выбранный bin перемещён объект из другой привязки: {other}.")
            if current_bin in folders and other_bin in folders:
                require(other_bin not in self.ancestors(folders[current_bin]) and
                        current_bin not in self.ancestors(folders[other_bin]),
                        f"Bin пересекается с другой привязкой: {folders[other_bin].GetName()}.")

    def detach(self):
        """Disable the current binding while retaining its snapshots and journal."""
        require(self.active_slot, "Выбери привязку, которую нужно отключить.")
        record = self.bindings[self.active_slot]
        marker = self.base / (self.active_slot + ".detached")
        write_json(marker, {"root": record["root"], "bin": record.get("bin")})
        del self.bindings[self.active_slot]
        self.select(None)

    def snapshot(self, root, root_id, linked_id):
        project = self.manager.GetCurrentProject()
        pool = project.GetMediaPool() if project else None
        require(pool and pool.GetUniqueId() == self.pool_id, "Открыт другой проект Resolve.")
        self.project, self.pool = project, pool
        return disk_tree(root, root_id), resolve_tree(self.pool, linked_id, root.name)

    def interrupted_import(self):
        """Allow resuming an initial run that only added items to the new bin."""
        if not self.pending.exists():
            return None
        journal = json.loads(self.pending.read_text(encoding="utf-8"))
        require(not self.binding and journal.get("bin") and not journal.get("relinks") and
                all(side == "Bin" and action == "add" for side, action, *_ in journal["operations"]),
                f"Предыдущая операция прервана. Журнал: {self.pending}")
        return journal

    def inspect(self, selected):
        self.preview = None
        interrupted = self.interrupted_import()
        saved = self.binding
        if saved:
            root, root_id, linked_id = Path(saved["root"]), saved["root_id"], saved["bin"]
        elif interrupted:
            root = Path(interrupted["root"])
            root_id = interrupted.get("root_id") or identity(root.lstat())
            linked_id = interrupted["bin"]
        else:
            require(selected.strip(), "Выбери папку на диске.")
            root = Path(selected).expanduser().resolve(strict=True)
            require(root.name and root.is_dir(), "Выбери директорию внутри диска.")
            component(root.name)
            root_id, linked_id = identity(root.stat()), self.attach_id
        disk, (_, bins, folders, clips) = self.snapshot(root, root_id, linked_id)
        self.check_scope(root, root_id, linked_id, folders, bins)
        previous = saved["entries"] if saved else {}
        plan = make_plan(previous, disk, bins, folders, clips, root)
        preflight(root, self.project, previous, disk, bins, clips, plan)
        left, right, kinds, targets = plan
        ops = schedule(left, targets, kinds, "Диск") + schedule(right, targets, kinds, "Bin")
        messages = []
        if interrupted:
            planned_paths = {op[4] for op in interrupted["operations"]}
            require(set(bins) <= planned_paths and
                    all(side == "Bin" and action == "add" for side, action, *_ in ops),
                    f"Созданный bin изменился после сбоя. Журнал: {self.pending}")
            messages.append(f"Продолжить первый импорт в bin «{root.name}»")
        if not linked_id:
            messages.append(f"Bin: создать корневой bin «{root.name}»")
        elif not saved and not interrupted:
            messages.append(f"Привязать выбранный bin к {root}")
        for side, action, key, source, dest in ops:
            noun = "папку" if kinds[key] == "bin" else "медиа"
            if action == "move":
                text = f"переместить {noun} {source} → {dest}"
            elif action == "add":
                if kinds[key] == "bin":
                    text = f"создать папку {dest}"
                elif side == "Bin":
                    text = f"импортировать {dest}"
                else:
                    text = f"скопировать {bins[right[key]].source} → {dest}"
            else:
                verb = "убрать в папку восстановления" if side == "Диск" and kinds[key] == "clip" else "удалить"
                text = f"{verb} {noun} {source}"
            messages.append(f"{side}: {text}")
        relinks = [key for key in right if key in targets and kinds[key] == "clip" and
                   bins[right[key]].source != str(root / targets[key])]
        for key in relinks:
            messages.append(f"Bin: перелинковать {right[key]} на {targets[key]}")
        self.preview = (root, root_id, linked_id, disk, bins, plan, ops, relinks, interrupted)
        return "\n".join(messages) or "Изменений нет."

    def apply(self):
        require(self.preview, "Сначала построй план.")
        root, root_id, linked_id, old_disk, old_bins, plan, ops, relinks, interrupted = self.preview
        self.preview = None
        require(self.interrupted_import() == interrupted, "Журнал изменился после предпросмотра.")
        disk, (linked, bins, folders, clips) = self.snapshot(root, root_id, linked_id)
        self.check_scope(root, root_id, linked_id, folders, bins)
        require(disk == old_disk and bins == old_bins,
                "Диск или bin изменились после предпросмотра. Построй план заново.")
        preflight(root, self.project, self.binding["entries"] if self.binding else {},
                  disk, bins, clips, plan)
        left, right, kinds, targets = plan
        if not ops and not relinks and self.binding:
            expected = {dest: {"kind": kinds[key], "disk": disk[dest].id,
                               "bin": bins[dest].id, "stamp": list(disk[dest].stamp)}
                        for key, dest in targets.items()}
            if expected == self.binding["entries"]:
                return "Изменений нет."

        batch = root / RECOVERY / uuid.uuid4().hex
        journal = {"root": str(root), "root_id": root_id, "bin": linked_id,
                   "operations": interrupted["operations"] + ops if interrupted else ops,
                   "relinks": [targets[key] for key in relinks], "recovery": str(batch)}
        write_json(self.pending, journal)
        self.active_slot = self.slot
        self.bindings[self.slot] = self.binding or journal
        handles = {key: bins[path].obj for key, path in right.items()}
        bin_paths = dict(right)
        rejected = {}
        original = self.pool.GetCurrentFolder()
        step = "создание корневого bin"
        try:
            if not linked_id:
                linked = self.pool.AddSubFolder(linked, root.name)
                require(linked, "Resolve не создал корневой bin.")
                linked_id = linked.GetUniqueId()
                journal["bin"] = linked_id
                write_json(self.pending, journal)
                # Первое редактирование может сохранить новый проект и обновить объекты API.
                _, (linked, _, _, _) = self.snapshot(root, root_id, linked_id)

            def folder_at(path):
                if not path:
                    return linked
                return next(handles[key] for key, current in bin_paths.items() if current == path)

            for side, action, key, old, new in ops:
                step = f"{side}: {action} {old or ''} {new or ''}"
                print(f"[Bin Sync] {step}")
                is_bin = kinds[key] == "bin"
                if side == "Диск":
                    source, dest = (root / old if old else None), (root / new if new else None)
                    if source:
                        info = source.lstat()
                        expected = disk[left[key]]
                        require(source.resolve(strict=True) == source and identity(info) == expected.id and
                                (is_bin or (info.st_size, info.st_mtime_ns) == expected.stamp),
                                f"Объект на диске изменился во время синхронизации: {source}.")
                    if dest:
                        require(dest.parent.resolve(strict=True) == dest.parent,
                                f"Папка назначения стала символической ссылкой: {dest.parent}")
                    if action == "add":
                        if is_bin:
                            dest.mkdir()
                        else:
                            copy_noreplace(Path(bins[right[key]].source), dest)
                    elif action == "move":
                        move_noreplace(source, dest)
                    elif is_bin:
                        source.rmdir()
                    else:
                        recovery_path = batch / old
                        require(recovery_path.parent.resolve() == recovery_path.parent,
                                "Папка восстановления стала символической ссылкой.")
                        recovery_path.parent.mkdir(parents=True, exist_ok=True)
                        move_noreplace(source, recovery_path)
                    continue
                obj = handles.get(key)
                if action == "remove":
                    if is_bin:
                        require(obj.GetSubFolderList() == [] and obj.GetClipList() == [],
                                f"Bin не пуст: {old}.")
                        ok = self.pool.DeleteFolders([obj])
                    else:
                        ok = self.pool.DeleteClips([obj])
                    require(ok, f"Resolve не удалил {old}.")
                    del bin_paths[key]
                    continue
                parent = folder_at(parent_of(new))
                if action == "add":
                    if is_bin:
                        obj = self.pool.AddSubFolder(parent, PurePosixPath(new).name)
                        require(obj, f"Resolve не создал bin {new}.")
                    else:
                        obj, detail = import_file(self.pool, self.storage, parent, root / new)
                        if obj is None:
                            rejected[key] = new
                            print(f"[Bin Sync] Resolve не принял {root / new}. {detail}")
                            continue
                elif not is_bin:
                    if parent_of(old) != parent_of(new):
                        require(self.pool.MoveClips([obj], parent), f"Resolve не переместил {old}.")
                elif PurePosixPath(old).name == PurePosixPath(new).name:
                    require(self.pool.MoveFolders([obj], parent), f"Resolve не переместил bin {old}.")
                else:
                    # В установленном API нет Folder.SetName. Переносим содержимое с сохранением клипов.
                    replacement = self.pool.AddSubFolder(parent, PurePosixPath(new).name)
                    require(replacement, f"Resolve не создал bin {new}.")
                    children, items = obj.GetSubFolderList(), obj.GetClipList()
                    require(children is not None and items is not None, f"Resolve не прочитал bin {old}.")
                    require(not children or self.pool.MoveFolders(children, replacement),
                            f"Resolve не перенёс подбины из {old}.")
                    require(not items or self.pool.MoveClips(items, replacement),
                            f"Resolve не перенёс клипы из {old}.")
                    require(obj.GetSubFolderList() == [] and obj.GetClipList() == [] and
                            self.pool.DeleteFolders([obj]), f"Resolve не удалил старый bin {old}.")
                    obj = replacement
                if action == "move" and is_bin:
                    bin_paths = {k: new + p[len(old):] if under(p, old) else p for k, p in bin_paths.items()}
                handles[key], bin_paths[key] = obj, new

            for key in relinks:
                step = f"перелинковка {targets[key]}"
                retarget(self.pool, handles[key], root / targets[key])

            step = "сверка итогового дерева и сохранение проекта"
            final_disk, (_, final_bins, _, _) = self.snapshot(root, root_id, linked_id)
            accepted_paths = {dest for key, dest in targets.items() if key not in rejected}
            require(set(final_disk) == set(targets.values()) and set(final_bins) == accepted_paths,
                    "После применения списки файлов и bin различаются.")
            entries = {}
            for key, dest in targets.items():
                if key in rejected:
                    continue
                file, entry = final_disk[dest], final_bins[dest]
                require(file.kind == entry.kind == kinds[key] and
                        (entry.kind == "bin" or entry.source == str(root / dest)),
                        f"После применения не совпадает источник или тип {dest}.")
                require(entry.id == handles[key].GetUniqueId(), f"Resolve изменил ID {dest}.")
                entries[dest] = {"kind": file.kind, "disk": file.id, "bin": entry.id, "stamp": list(file.stamp)}
            require(self.manager.SaveProject(), "Resolve не сохранил проект.")
            binding = {"version": 1, "root": str(root), "root_id": root_id, "bin": linked_id,
                       "entries": entries}
            write_json(self.state, binding)
            self.binding = binding
            self.active_slot = self.slot
            self.bindings[self.slot] = binding
            self.pending.unlink()
        except Exception as exc:
            raise RuntimeError(f"{exc}\nЭтап: {step}\nОперация прервана. Журнал: {self.pending}") from exc
        finally:
            if original:
                try:
                    self.pool.SetCurrentFolder(original)
                except Exception as exc:
                    print(f"[Bin Sync] Не удалось вернуть прежний bin: {exc}")
        message = f"Синхронизировано элементов: {len(entries)}."
        if rejected:
            message += "\nResolve не импортировал:\n" + "\n".join(rejected.values())
            if any(is_still(path) for path in rejected.values()):
                message += "\nДля отдельных кадров включи Show Individual Frames в Media Storage."
            message += "\nПодробности в Workspace → Console. При следующем запуске импорт можно повторить."
        if batch.exists():
            message += f"\nВосстановление файлов: {batch}"
        return message


def main():
    """Open the Resolve Studio UI from Workspace > Scripts."""
    bridge = globals().get("bmd")
    if not bridge:
        import fusionscript as bridge
    app = globals().get("resolve") or bridge.scriptapp("Resolve")
    require(app, "Resolve Scripting API недоступен.")
    manager = app.GetProjectManager()
    project = manager.GetCurrentProject()
    require(project, "Открой проект Resolve.")
    fusion = app.Fusion()
    ui = fusion.UIManager
    require(ui, "Для окна Bin Sync нужен UIManager Resolve Studio.")
    dispatcher = bridge.UIDispatcher(ui)
    session = Session(manager, project, app.GetMediaStorage())
    try:
        window = dispatcher.AddWindow(
            {"ID": "BinSync", "WindowTitle": "Bin Sync — " + project.GetName(),
             "Geometry": [160, 120, 780, 340]},
            ui.VGroup({"Spacing": 6}, [
                ui.Label({"ID": "Status", "WordWrap": True, "Weight": 0}),
                ui.ComboBox({"ID": "Bindings", "Weight": 0}),
                ui.HGroup({"Weight": 0, "Spacing": 6}, [
                    ui.LineEdit({"ID": "Path", "PlaceholderText": "Папка на диске", "Weight": 1}),
                    ui.Button({"ID": "Browse", "Text": "Выбрать…", "Weight": 0}),
                ]),
                ui.TextEdit({"ID": "Log", "ReadOnly": True, "Weight": 1}),
                ui.HGroup({"Weight": 0, "Spacing": 6}, [
                    ui.Button({"ID": "Preview", "Text": "Показать изменения", "Weight": 0}),
                    ui.Button({"ID": "Apply", "Text": "Применить и сохранить проект", "Weight": 0}),
                    ui.Button({"ID": "Detach", "Text": "Отключить привязку", "Weight": 0}),
                    ui.HGap(0, 1),
                    ui.Button({"ID": "Exit", "Text": "Закрыть", "Weight": 0}),
                ]),
            ]),
        )
        items = window.GetItems()
        root_selected = session.selected_id == session.master_id
        slots = []
        updating = False
        confirming_detach = False
        preview_path = None
        folder_names = {}
        items["Bindings"].Visible = root_selected

        def reset_confirmation():
            nonlocal confirming_detach
            confirming_detach = False
            items["Detach"].Text = "Отключить привязку"

        def refresh_choices():
            """Rebuild the selector only when the set of bindings changes."""
            nonlocal updating
            if not root_selected:
                return
            desired = [None, *sorted(session.bindings, key=lambda slot: session.bindings[slot]["root"])]
            updating = True
            try:
                if slots != desired:
                    slots[:] = desired
                    folder_names.clear()
                    stack = [session.pool.GetRootFolder()]
                    while stack:
                        folder = stack.pop()
                        uid = folder.GetUniqueId()
                        if uid in folder_names:
                            continue
                        folder_names[uid] = folder.GetName()
                        stack.extend(folder.GetSubFolderList() or [])
                    items["Bindings"].Clear()
                    items["Bindings"].AddItem("Новая привязка")
                    for slot in slots[1:]:
                        linked = session.bindings[slot]
                        name = folder_names.get(linked.get("bin"), "Bin отсутствует")
                        items["Bindings"].AddItem(f"{name}  ↔  {linked['root']}")
                index = slots.index(session.active_slot)
                if int(items["Bindings"].CurrentIndex) != index:
                    items["Bindings"].CurrentIndex = index
            finally:
                updating = False

        def render_selection():
            nonlocal preview_path
            preview_path = None
            record = session.bindings.get(session.active_slot)
            if record:
                name = folder_names.get(record.get("bin"), "Bin отсутствует") if root_selected else session.selected_name
                status = f"Выбрано: {name}. Директория привязки: {record['root']}"
                if session.pending.exists():
                    status += "\nЕсть журнал незавершённой операции."
            elif root_selected:
                status = "Корень Media Pool. Выбери папку для нового bin или привязку из списка."
            else:
                status = f"Bin «{session.selected_name}» не синхронизируется. Выбери папку, чтобы привязать его."
            items["Status"].Text = status
            items["Path"].Text = record["root"] if record else ""
            items["Path"].ReadOnly = bool(record)
            items["Browse"].Enabled = record is None
            items["Detach"].Enabled = bool(record)
            items["Apply"].Enabled = False
            reset_confirmation()

        def guarded(action):
            def handler(event):
                try:
                    action()
                except Exception as exc:
                    if session.active_slot and session.pending.exists():
                        refresh_choices()
                        render_selection()
                    items["Apply"].Enabled = False
                    items["Log"].PlainText = str(exc)
                    print(f"[Bin Sync] {exc}")
            return handler

        def browse():
            change_binding()
            selected = fusion.RequestDir()
            if isinstance(selected, str) and selected:
                items["Path"].Text = selected

        def change_binding():
            if not updating and root_selected:
                index = int(items["Bindings"].CurrentIndex)
                if 0 <= index < len(slots) and slots[index] != session.active_slot:
                    session.select(slots[index])
                    items["Log"].PlainText = ""
                    render_selection()

        def preview():
            nonlocal preview_path
            change_binding()
            reset_confirmation()
            path = items["Path"].Text
            items["Log"].PlainText = session.inspect(path)
            preview_path = path
            items["Apply"].Enabled = True

        def apply():
            change_binding()
            reset_confirmation()
            items["Apply"].Enabled = False
            require(preview_path == items["Path"].Text,
                    "Путь изменился. Нажми «Показать изменения» ещё раз.")
            message = session.apply()
            refresh_choices()
            render_selection()
            items["Log"].PlainText = message

        def detach():
            nonlocal confirming_detach
            change_binding()
            require(session.active_slot, "Выбери привязку, которую нужно отключить.")
            record = session.bindings[session.active_slot]
            if not confirming_detach:
                confirming_detach = True
                session.preview = None
                items["Apply"].Enabled = False
                items["Detach"].Text = "Подтвердить отключение"
                items["Log"].PlainText = (
                    f"Отключить синхронизацию с {record['root']}? "
                    "Bin, клипы и файлы останутся на месте."
                )
                return
            session.detach()
            refresh_choices()
            render_selection()
            items["Log"].PlainText = "Привязка отключена. Bin, клипы и файлы сохранены."

        refresh_choices()
        render_selection()
        window.On.Bindings.CurrentIndexChanged = guarded(change_binding)
        window.On.Browse.Clicked = guarded(browse)
        window.On.Preview.Clicked = guarded(preview)
        window.On.Apply.Clicked = guarded(apply)
        window.On.Detach.Clicked = guarded(detach)
        window.On.Exit.Clicked = lambda event: dispatcher.ExitLoop()
        window.On.BinSync.Close = lambda event: dispatcher.ExitLoop()
        window.Show()
        try:
            dispatcher.RunLoop()
        finally:
            window.Hide()
    finally:
        session.lock.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"[Bin Sync] {error}")
