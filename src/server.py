#!/usr/bin/env python3

import argparse
import queue
import collections.abc
import dataclasses
import datetime
import enum
import json
import logging
import pathlib
import pprint
import sqlite3
import subprocess
import sys
import threading
import types
import uuid
import zipfile
from typing import Any, Callable, Dict, List, Optional, Union

import flask

# ----------------------------------------------------------------------------------------------------------
#   Globals
# ----------------------------------------------------------------------------------------------------------

__VERSION__ = "0.1.0"

logger: logging.Logger = logging.getLogger(__name__)
app: flask.Flask = flask.Flask(__name__)

# ----------------------------------------------------------------------------------------------------------
#   General defs and helpers
# ----------------------------------------------------------------------------------------------------------


@dataclasses.dataclass
class ResultsEntry:
    repo: str  # Assuming RepositoryData can be serialized to str
    id: str  # Storing UUID as string
    status: str  # Assuming ReturnCode can be serialized to str
    results: List[str]  # Assuming TaskResult can be serialized to List[str]
    timestamp: str  # Timestamp as string
    branch: str
    commit: str


@dataclasses.dataclass
class RepositoryData:
    name: str
    timestamp: datetime.datetime
    path: pathlib.Path
    url: str
    sha: str
    branch: str


def load_json(path_json: Union[str, pathlib.Path]) -> Dict[str, Any]:
    with pathlib.Path(path_json).open("r", encoding="utf-8") as f:
        return json.loads(f.read())


def dict_to_dataclass(data: dict) -> dataclasses.dataclass:
    fields = [(key, type(value)) for key, value in data.items()]
    dynamic_dataclass = dataclasses.make_dataclass("DynamicDataclass", fields)
    return dynamic_dataclass(**data)


def flatten_dataclass(dataclass: dataclasses.dataclass, parent_key="", sep="_") -> dataclasses.dataclass:
    data = dataclasses.asdict(dataclass)
    items = []

    for key, value in data.items():
        new_key = parent_key + sep + key if parent_key else key

        if isinstance(value, collections.abc.Mapping):
            items.extend(flatten_dataclass(value, new_key, sep=sep).items())
        else:
            items.append((new_key, value))

    return dict_to_dataclass(dict(items))


def run_and_check(cmd: str, cwd: Optional[Union[str, pathlib.Path]] = None, timeout: int = 7200) -> str:
    logger.debug("Running cmd ´%s´ under path ´%s´", cmd, cwd)
    result = subprocess.run(
        cmd,
        shell=True,
        check=True,
        capture_output=True,
        cwd=cwd,
        timeout=timeout,
    )
    return result.stdout.decode("utf-8")


# ----------------------------------------------------------------------------------------------------------
#   Executor
# ----------------------------------------------------------------------------------------------------------


class ExecutorThread(threading.Thread):
    """
    Executes Pipeline objects
    """

    def __init__(self, pipeline, mutex: Optional[threading.Lock] = None) -> None:
        super().__init__(daemon=False)  # NOTE: wait for thread to finish task before exiting
        self._pipeline: Pipeline = pipeline
        self._mutex: Optional[threading.Lock] = mutex
        self._exit_requested: bool = False

    def stop(self) -> None:
        self._exit_requested = True

    def run(self) -> None:
        if self._mutex is not None:
            self._mutex.acquire()

        logger.debug("Worker %s started execution", repr(self))

        if self._exit_requested:  # NOTE: check after waiting period
            logger.debug("Worker %s stopped execution", repr(self))

            if self._mutex is not None:
                self._mutex.release()

            return

        logger.debug("Worker %s started working", repr(self))

        self._set_pipeline_status(ReturnCode.RUNNING)

        for stage in self.stages:
            for task in stage["tasks"]:
                logger.info("Executing task %s", task.name)
                task.eval()

                if task.status is not ReturnCode.SUCCESSFUL:
                    try:
                        logger.debug(
                            "Stage %s of pipeline %s failed. Skipping..", stage["name"], self._pipeline.name
                        )
                    except:
                        pass

                    break

                if self._exit_requested:
                    break

            if self._exit_requested:
                break

        self._update_pipeline_status()

        logger.debug("Worker %s stopped working", repr(self))

        logger.debug(
            "Results of pipeline %s are %s", self._pipeline.name, pprint.pformat(self._pipeline.results)
        )
        logger.info("Pipeline %s finished with status %s", self._pipeline.name, self._pipeline.status)

        logger.debug("Worker %s stopped execution", repr(self))

        if self._mutex is not None:
            self._mutex.release()

    def _set_pipeline_status(self, value) -> None:
        self._pipeline.status = value

    def _update_pipeline_status(self) -> None:
        rcs = {task.status for task in self._pipeline.tasks}
        logger.debug("Unique status values of pipeline %s are %s", self._pipeline.name, rcs)

        if ReturnCode.FAILED in rcs:
            self._pipeline.status = ReturnCode.FAILED
            return

        if ReturnCode.UNKNOWN in rcs:
            self._pipeline.status = ReturnCode.UNKNOWN
            return

        self._pipeline.status = ReturnCode.SUCCESSFUL

    @property
    def pipeline(self):
        return self._pipeline

    @property
    def stages(self):
        return self._pipeline.stages

    @property
    def results(self):
        return self._pipeline.results

    @property
    def name(self):
        return self._pipeline.name

    @property
    def status(self):
        return self._pipeline.status


class Executor:
    """
    Collects and manages threads of type ExecutorThread
    """

    def __init__(self, database: "Database") -> None:
        self._mutex = threading.Lock()
        self._threads: List[ExecutorThread] = []
        self.db = database

    def __del__(self) -> None:
        for thread in self._threads:
            thread.join()

        self.clean()

    def enqueue(self, pipeline: "Pipeline") -> None:
        thread = ExecutorThread(pipeline=pipeline, mutex=self._mutex)
        self._threads.append(thread)
        thread.start()
        self.clean()

    def clean(self) -> None:
        dead_threads = [thread for thread in self._threads if not thread.is_alive()]

        for thread in dead_threads:
            results_entry = thread.pipeline.to_results_entry()

            if results_entry:
                self.db.add_results(results_entry)

        self._threads = [thread for thread in self._threads if thread.is_alive()]
        logger.debug("Removed %s dead threads", len(dead_threads))
        logger.debug("Thread list updated to %s", self._threads)

    def status(self, sha=None, id_=None) -> None:
        # TODO: Implement method to return the status based on sha or id
        pass


# ----------------------------------------------------------------------------------------------------------
#   Pipeline
# ----------------------------------------------------------------------------------------------------------


class Stage(enum.Enum):
    SETUP = 0
    PRE = 1
    SCRIPT = 2
    POST = 3
    ARTIFACT = 4

    def __str__(self) -> str:
        return str(self.name.upper())

    def __int__(self) -> int:
        return int(self.value)


class ReturnCode(enum.Enum):
    SUCCESSFUL = 0
    FAILED = 1
    STOPPED = 2
    RUNNING = 3
    UNKNOWN = 4

    def __bool__(self) -> bool:
        if self == ReturnCode.SUCCESSFUL:
            return True

        return False

    def __str__(self) -> str:
        return str(self.name.upper())

    def __int__(self) -> int:
        return int(self.value)


class TaskResult:
    def __init__(self) -> None:
        self._error = None
        self._value = None

    @classmethod
    def from_error(cls, error: Any):
        instance = cls()
        instance._error = error
        return instance

    @classmethod
    def from_value(cls, value: Any):
        instance = cls()
        instance._value = value
        return instance

    def unwrap(self) -> Any:
        if self._error is not None:
            raise ValueError(f"Task failed with error: {self._error}")

        return self._value

    def __str__(self) -> str:
        if self._error is not None:
            return f"Error: {self._error}"

        return str(self._value)

    __repr__ = __str__


class Task:
    def __init__(self, closure, name: Optional[str] = None) -> None:
        self._id = uuid.uuid4()
        self._name: Optional[str] = name
        self._closure: Callable = closure
        self._result: Optional[TaskResult] = None
        self._rc: ReturnCode = ReturnCode.UNKNOWN

        if self._name is None:
            self._name = str(self._id)

    def eval(self) -> None:
        logger.debug("Evaluating task %s in thread %s", self._name, threading.current_thread())

        try:
            result = self._closure()
            self._result = TaskResult.from_value(result)
            logger.debug("Finished task %s successfully", self._name)
            self._rc = ReturnCode.SUCCESSFUL
        except Exception as err:
            self._result = TaskResult.from_error(f"Task {self._name} could not be evaluated: {err}")
            logger.exception("Task %s could not be evaluated!", self._name)
            self._rc = ReturnCode.FAILED

    @classmethod
    def from_callable(cls, func, args=None, kwargs=None, name=None):
        if args is None:
            args = []

        if kwargs is None:
            kwargs = {}

        def closure() -> Any:
            return func(*args, **kwargs)

        return cls(closure, name=name)

    @classmethod
    def from_cmd(cls, cmd: str, cwd=None, name=None):
        if name is None:
            name = f'"{cmd}"'

        return cls.from_callable(run_and_check, args=[cmd, cwd], name=name)

    @property
    def name(self) -> str:
        return self._name

    @property
    def result(self) -> TaskResult:
        if self._result is None:
            return TaskResult.from_error(f"<Task {self._name} was not evaluated>")

        return self._result

    @property
    def status(self) -> ReturnCode:
        return self._rc


class Pipeline:
    NAME_CONFIGFILE = pathlib.Path("pipeline.json")

    def __init__(
        self,
        *,
        repo: RepositoryData,
        config: Dict[str, Any],
        args: argparse.Namespace,
        name: Optional[str] = None,
    ) -> None:
        self._name = name
        self._config = config
        self._repo = repo
        self._args = args

        self._id = uuid.uuid4()
        self._status: ReturnCode = ReturnCode.STOPPED

        if self._name is None:
            self._name = str(self._id)

        self._stages: List[Dict[str, Any]] = sorted(
            [{"stage": stage, "tasks": []} for stage in Stage],
            key=lambda x: int(x["stage"]),
        )
        self._parse_tasks()

    @classmethod
    def from_dict(cls, config: Dict) -> Optional["Pipeline"]:
        try:
            name = config.get("name", None)
            repo = config.get("repo", None)
            args = config.get("args", None)

            if name is None or repo is None or args is None:
                raise ValueError("Missing required fields in the configuration.")

            return cls(repo=repo, name=name, config=config, args=args)
        except Exception:
            logger.exception("Failed to create Pipeline instance from dictionary!")

        return None

    @classmethod
    def from_json(cls, path: Union[pathlib.Path, str]):
        config: Dict[str, Any] = load_json(path)

        if not isinstance(config, dict):
            raise ValueError("Loaded json data are of wrong format")

        return cls.from_dict(config=config)

    def add_task(self, task: Task, stage: Stage) -> None:
        index = int(stage)
        self._stages[index]["tasks"].append(task)

    def _load_tasks(self) -> None:
        config: Dict[str, Any] = load_json(self._repo.path.joinpath(self.NAME_CONFIGFILE))

        if not isinstance(config, dict):
            raise ValueError("Loaded json data are of wrong format")

        self._config.update(config)

    def _assemble_tasks(self) -> None:
        config_stages = self._config.get("stages")

        if config_stages is None:
            raise ValueError("Failed parsing config")

        for name_stage in ["pre", "script", "post"]:
            config_stage = config_stages.get(name_stage)

            if not config_stage:
                continue

            stage = Stage[name_stage.upper()]

            for cmd in config_stage:
                self.add_task(Task.from_cmd(cmd, self._repo.path), stage=stage)

        config_artifact = config_stages.get("artifact")

        if config_artifact:
            self.add_task(
                Task.from_callable(
                    plugin_file_collect,
                    args=[config_artifact, self._repo, self._args],
                    name="Collect artifacts",
                ),
                stage=Stage.ARTIFACT,
            )

        logger.debug(
            "Assembled task list %s for pipeline %s",
            [task.name for stage in self.stages for task in stage["tasks"]],
            self._name,
        )

    def _parse_tasks(self) -> None:
        self.add_task(
            Task.from_callable(plugin_git_prepare, args=[self._repo], name=f"Prepare {self._repo.name}"),
            stage=Stage.SETUP,
        )

        if self._config.get("stages") is None:
            self.add_task(Task.from_callable(self._load_tasks, name="Load pipeline.json"), stage=Stage.SETUP)

        self.add_task(Task.from_callable(self._assemble_tasks, name="Assemble tasks"), stage=Stage.SETUP)

    def to_results_entry(self) -> ResultsEntry:
        return ResultsEntry(
            repo=self._repo.name,
            id=str(self._id),  # TODO: use marshmallow?
            status=str(self._status),
            results=[str(result) for result in self.results],
            timestamp=datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            branch=self._repo.branch,
            commit=self._repo.sha,  # Using SHA as the commit identifier
        )

    @property
    def name(self):
        return self._name

    @property
    def stages(self):
        return self._stages

    @property
    def id(self):
        return self._id

    @property
    def results(self) -> list:
        return [task.result for stage in self.stages for task in stage["tasks"]]

    @property
    def tasks(self):
        return [task for stage in self.stages for task in stage["tasks"]]

    @property
    def status(self) -> ReturnCode:
        return self._status

    @status.setter
    def status(self, value: ReturnCode):
        self._status = value


# ----------------------------------------------------------------------------------------------------------
#   Database
# ----------------------------------------------------------------------------------------------------------

# repo: str  # Assuming RepositoryData can be serialized to str
# id: str  # Storing UUID as string
# status: str  # Assuming ReturnCode can be serialized to str
# results: List[str]  # Assuming TaskResult can be serialized to List[str]
# timestamp: str  # Timestamp as string
# branch: str
# commit: str


class Database:
    DEFAULT_DATABASE_FILE = pathlib.Path("server.db")

    def __init__(self, path_databasefile: Optional[pathlib.Path]) -> None:
        if path_databasefile is None:
            path_databasefile = self.DEFAULT_DATABASE_FILE

        self._database = sqlite3.connect(path_databasefile)
        self._cursor = self._database.cursor()
        self._create_table()

    def _create_table(self) -> None:
        self._cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS Results (
                id TEXT PRIMARY KEY,
                repo TEXT,
                status TEXT,
                results TEXT,
                timestamp TEXT,
                branch TEXT,
                "commit" TEXT,
                filepath TEXT
            );
        """
        )
        self._database.commit()

    def __del__(self) -> None:
        self._database.close()

    def add_results(self, entry: ResultsEntry) -> bool:
        # Check for conflicting UUID
        if self.find_results_by_id(entry.id):
            return False  # UUID conflict

        # Add timestamp
        # entry.timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

        entry_dict = dataclasses.asdict(entry)
        cmd = """
            INSERT INTO Results (id, repo, status, results, timestamp, branch, commit)
            VALUES (:id, :repo, :status, :results, :timestamp, :branch, :commit);
        """
        self._cursor.execute(cmd, entry_dict)
        self._database.commit()
        return True

    def find_results_by_id(self, id_: str) -> Optional[ResultsEntry]:
        cmd = "SELECT * FROM Results WHERE id = ?;"
        self._cursor.execute(cmd, (id,))
        row = self._cursor.fetchone()

        if row is not None:
            return self.ResultsEntry(
                repo=row[1],
                id=row[0],
                status=row[2],
                results=row[3].split(","),
                timestamp=row[4],
                branch=row[5],
                commit=row[6],
            )

        return None

    def find_results_by_repo_info(self, name: str, branch: str, commit: str) -> List[ResultsEntry]:
        cmd = "SELECT * FROM Results WHERE repo = ? AND branch = ? AND commit = ?;"
        self._cursor.execute(cmd, (name, branch, commit))
        rows = self._cursor.fetchall()
        results = []

        for row in rows:
            results.append(
                self.ResultsEntry(
                    repo=row[1],
                    id=row[0],
                    status=row[2],
                    results=row[3].split(","),
                    timestamp=row[4],
                    branch=row[5],
                    commit=row[6],
                )
            )

        return results

    def find_results_by_timestamp(self, timestamp: str) -> List[ResultsEntry]:
        cmd = "SELECT * FROM Results WHERE timestamp > ?;"
        self._cursor.execute(cmd, (timestamp,))
        rows = self._cursor.fetchall()
        results = []

        for row in rows:
            results.append(
                self.ResultsEntry(
                    repo=row[1],
                    id=row[0],
                    status=row[2],
                    results=row[3].split(","),
                    timestamp=row[4],
                    branch=row[5],
                    commit=row[6],
                )
            )

        return results

    def find_last_entry_by_repo_info(self, name: str, branch: str, commit: str) -> Optional[ResultsEntry]:
        cmd = """
            SELECT * FROM Results
            WHERE repo = ? AND branch = ? AND commit = ?
            ORDER BY timestamp DESC
            LIMIT 1;
        """
        self._cursor.execute(cmd, (name, branch, commit))
        row = self._cursor.fetchone()

        if row is not None:
            return self.ResultsEntry(
                repo=row[1],
                id=row[0],
                status=row[2],
                results=row[3].split(","),
                timestamp=row[4],
                branch=row[5],
                commit=row[6],
            )

        return None

    def get_last_n_entries(self, n):
        self._cursor.execute("SELECT * FROM entries ORDER BY timestamp DESC LIMIT ?", (n,))
        rows = self._cursor.fetchall()
        result = []

        for row in rows:
            result.append({"id": row[0], "file_path": row[1], "timestamp": row[2]})

        return result


# ----------------------------------------------------------------------------------------------------------
#   Plugins for pipelines
#
#   Dont catch Exceptions here or make sure to re-raise them in order to allow proper
#   reporting inside Task class.
#
#   Also use the "plugin_" prefix as a naming convention and give full type hints for
#   new plugins
# ----------------------------------------------------------------------------------------------------------


def plugin_save_logfile() -> None:
    pass  # TODO: save stdout/stderr to logfile in artifacts


def plugin_file_collect(files: List[str], repo: RepositoryData, args: argparse.Namespace) -> None:
    path_zip = args.output_dir.joinpath(repo.name, repo.branch, repo.sha + ".zip")
    path_zip.parent.mkdir(parents=True, exist_ok=True)
    logger.debug("Opening artifact archive %s", path_zip)
    skipped_files = 0

    with zipfile.ZipFile(path_zip, "w") as archive:
        for file in files:
            path_file = pathlib.Path(file).resolve()
            path_repo = args.input_dir.joinpath(repo.name).resolve()

            if not path_file.is_absolute():  # NOTE: assume its relative to repo
                path_file = path_repo.joinpath(file)

            if not path_file.exists():
                logger.debug("File %s does not exist. Skipping..", path_file)
                skipped_files += 1
                continue

            logger.debug("Adding file %s to archive", path_file)

            if path_file.is_relative_to(path_repo):
                archive.write(path_file, arcname=path_file.relative_to(args.input_dir))
            else:  # NOTE: full path (except drive letter) is used inside archive
                archive.write(path_file)

    if skipped_files == len(files):  # NOTE: archive is empty
        path_zip.unlink()


def git_clean(repo: RepositoryData) -> None:
    run_and_check('git submodule foreach --recursive "git clean -xfd"', cwd=repo.path)
    run_and_check("git clean -xfd", cwd=repo.path)


def git_reset(repo: RepositoryData) -> None:
    run_and_check('git submodule foreach --recursive "git reset --hard"', cwd=repo.path)
    run_and_check("git reset --hard", cwd=repo.path)


def git_checkout(repo: RepositoryData) -> None:
    run_and_check("git submodule deinit --all -f", cwd=repo.path)  # NOTE: is needed  to avoid deadlocks
    run_and_check(f"git checkout --force {repo.sha}", cwd=repo.path)
    run_and_check("git submodule update --init --recursive", cwd=repo.path)


def git_pull(repo: RepositoryData) -> None:
    run_and_check(f"git checkout --force {repo.branch}", cwd=repo.path)
    run_and_check("git pull origin", cwd=repo.path)


def git_clone(repo: RepositoryData) -> None:
    run_and_check(f"git clone {repo.url} {repo.path}")


def git_status_commit(repo: RepositoryData) -> str:
    return run_and_check("git rev-parse HEAD", cwd=repo.path)


def plugin_git_prepare(repo: RepositoryData) -> None:
    if repo.path.exists():
        # TODO: if not git_status_commit(repo) == repo.sha:  # NOTE: clean old build results
        logger.info("Cleaning repo %s", repo.path)
        git_clean(repo)
        logger.info("Resetting repo %s", repo.path)
        git_reset(repo)
        logger.info("Pulling changes for repo %s", repo.path)
        git_pull(repo)
    else:
        logger.info("Cloning to %s", repo.path)
        git_clone(repo)

    logger.info("Checking out repo %s", repo.path)
    git_checkout(repo)


# ----------------------------------------------------------------------------------------------------------
#   Server
# ----------------------------------------------------------------------------------------------------------


class RequestData(types.SimpleNamespace):
    def __init__(self, dictionary, **kwargs) -> None:
        super().__init__(**kwargs)

        for key, value in dictionary.items():
            if isinstance(value, dict):
                self.__setattr__(key, RequestData(value))
            else:
                self.__setattr__(key, value)


class Server:
    DEFAULT_INPUT_DIR = pathlib.Path(r"C:\\inputs\\")
    DEFAULT_OUTPUT_DIR = pathlib.Path(r"C:\\outputs\\")

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args

        if args.input_dir is None:
            args.input_dir = self.DEFAULT_INPUT_DIR

        if args.output_dir is None:
            args.output_dir = self.DEFAULT_OUTPUT_DIR

        self.args.output_dir.mkdir(parents=True, exist_ok=True)
        self.args.input_dir.mkdir(parents=True, exist_ok=True)

        self.database = Database(path_databasefile=args.database_file)
        self.executor = Executor(database=self.database)

    def run(self) -> int:
        print("\n# -----------------------------------------------------------------------------")
        print(f"# Starting Server {__VERSION__}")
        print("# -----------------------------------------------------------------------------\n")
        app.run(debug=args.debug, host=args.host, port=args.port)
        return 0

    # --------------
    #   App routes
    # --------------

    @app.route("/gitlab", methods=["POST"])
    def route_lxi_framework(self):
        data = RequestData(json.loads(flask.request.data))
        logger.debug("Received data from Gitlab: %s", pprint.pformat(data))

        try:
            if not data.event_name == "push" or not data.object_kind == "push":
                raise ValueError(
                    f"Invalid webhook given with event {data._event_name} and kind {data.object_kind}"
                )

            repo = RepositoryData(
                name=data.repository.name,
                timestamp=datetime.datetime.now(),
                url=data.repository.git_http_url,
                sha=data.checkout_sha,
                path=args.input_dir.joinpath(data.repository.name),
                branch=data.ref.split("/")[-1],
            )
        except Exception:
            logger.exception("Failed parsing request!")
            return "Request is malformed", 400

        pipeline = Pipeline.from_dict({"repo": repo, "args": self.args})
        self.executor.enqueue(pipeline)
        logger.info(
            "Received commit %s from branch %s of project %s",
            repo.sha,
            repo.branch,
            repo.url,
        )
        return "OK", 200

    @app.route("/status/<string:name>/<string:branch>/<string:sha>", methods=["GET"])
    def route_status(self, name, branch, sha):  # TODO: use status flag of pipeline once we have a database
        logger.info("Received request for status repo %s, branch %s and sha %s", name, branch, sha)
        path_file = args.output_dir.joinpath(name, branch, sha + ".zip")
        response = {"status": "OK"}

        if not path_file.is_file():
            response["status"] = "NOK"

        return flask.jsonify(response)

    @app.route("/artifact/<string:name>/<string:branch>/<string:sha>", methods=["GET"])
    def route_artifact(self, name, branch, sha):
        logger.info("Received request for artifact repo %s, branch %s and sha %s", name, branch, sha)
        path_file = self.args.output_dir.joinpath(name, branch, sha + ".zip")

        if not path_file.is_file():
            return "Artifact is missing", 404

        return flask.send_file(path_file, as_attachment=True)

    @app.route("/artifacts", methods=["GET"])
    def route_artifacts(self) -> str:
        last_n_entries = self.database.get_last_n_entries(10)  # Assume this method exists in Database
        rows = ""

        for entry in last_n_entries:
            rows += (
                f"<tr><td>{entry['id']}</td><td>{entry['file_path']}</td><td>{entry['timestamp']}</td></tr>"
            )

        html: str = f"""
        <html>
            <head>
                <title>Last Entries</title>
            </head>
            <body>
                <h1>Last 10 Entries</h1>
                <table border=1>
                    <tr>
                        <th>ID</th>
                        <th>File Path</th>
                        <th>Timestamp</th>
                    </tr>
                    {rows}
                </table>
            </body>
        </html>
        """
        return html


# ----------------------------------------------------------------------------------------------------------
#   Entry point
# ----------------------------------------------------------------------------------------------------------


def str_to_bool(value: Union[str, bool]) -> bool:
    if isinstance(value, bool):
        return value
    if value.lower() in ("yes", "true", "t", "y", "1"):
        return True
    elif value.lower() in ("no", "false", "f", "n", "0"):
        return False
    else:
        raise argparse.ArgumentTypeError("Boolean value expected.")


def main(args: argparse.Namespace) -> int:
    server = Server(args)
    return server.run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-d",
        "--debug",
        type=str_to_bool,
        nargs="?",
        const=True,
        default=False,
        help="Activates debug mode.",
    )
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        nargs="?",
        const=6001,
        default=6001,
        help="Port of the server.",
    )
    parser.add_argument(
        "-a",
        "--host",
        type=str,
        nargs="?",
        const="0.0.0.0",
        default="0.0.0.0",
        help="Address of the server.",
    )
    parser.add_argument(
        "--input_dir",
        type=pathlib.Path,
        nargs="?",
        const=None,
        default=None,
        help="Sets workspace directory.",
    )
    parser.add_argument(
        "--output_dir",
        type=pathlib.Path,
        nargs="?",
        const=None,
        default=None,
        help="Sets artifact directory.",
    )
    parser.add_argument(
        "--database_file",
        type=pathlib.Path,
        nargs="?",
        const=None,
        default=None,
        help="Sets database file.",
    )
    parser.add_argument(
        "argv",
        nargs="*",
    )
    args = parser.parse_args()
    LOGGING_LEVEL = logging.DEBUG if args.debug else logging.ERROR
    LOGGING_FORMAT = "%(asctime)s | %(name)s | %(levelname)s | %(module)s, %(threadName)s, %(funcName)s, %(lineno)d | %(message)s"
    logging.basicConfig(format=LOGGING_FORMAT, level=LOGGING_LEVEL)
    return_code = main(args)
    logging.info("Done.")
    sys.exit(return_code)
