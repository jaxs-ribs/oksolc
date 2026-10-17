#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0
# Copyright (C) 2026 OKcontract Pte. Ltd.
"""Stateful oksolc-versus-solc differential campaign and reducer."""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import queue
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Any, Callable, Sequence


PREAMBLE = "// SPDX-License-Identifier: UNLICENSED\npragma solidity 0.8.36;\n"
ARTIFACTS = (
    ("evm.bytecode.object", "evm.deployedBytecode.object", "evm.methodIdentifiers"),
    ("evm.deployedBytecode.object",),
    ("abi", "evm.bytecode.object"),
)
OPTIMIZER_RUNS = (0, 1, 200, 1_000, 10_000)
RECURSIVE_SHAPES = (
    "contract Recursive { struct A { B[] children; } struct B { A parent; } }",
    "contract Recursive { struct A { mapping(uint256 => B) children; } struct B { A parent; } }",
    "contract Recursive { struct A { A[] children; } }",
    "contract Recursive { struct A { B[1] child; } struct B { A parent; } }",
    "contract Recursive { struct A { B[] children; } struct B { B parent; } }",
)
ACTIONS = (
    "first_body",
    "second_body",
    "signature_failure",
    "selective_reexport",
    "import_cycle",
    "imported_alias_swap",
    "recursive_shape",
    "alternate_source_names",
    "reverse_source_order",
    "extra_source",
    "optimizer_runs",
    "projection",
)


@dataclass
class ProjectState:
    first_body: int = 1
    second_body: int = 2
    signature_failure: bool = False
    selective_reexport: bool = False
    import_cycle: bool = False
    imported_alias_swap: bool = False
    recursive_shape: int = 0
    alternate_source_names: bool = False
    reverse_source_order: bool = False
    extra_source: bool = False
    optimizer_runs: int = 200
    projection: int = 0

    def mutate(self, rng: random.Random) -> None:
        action = rng.choice(ACTIONS)
        if action in ("first_body", "second_body"):
            setattr(self, action, rng.randint(1, 63))
        elif action == "recursive_shape":
            self.recursive_shape = rng.randrange(len(RECURSIVE_SHAPES))
        elif action == "optimizer_runs":
            self.optimizer_runs = rng.choice(OPTIMIZER_RUNS)
        elif action == "projection":
            self.projection = rng.randrange(len(ARTIFACTS))
        else:
            setattr(self, action, not getattr(self, action))


@dataclass
class Failure:
    kind: str
    revision: int
    path: tuple[str, ...]
    detail: str
    reference: Any = None
    candidate: Any = None
    candidate_fresh: Any = None
    session_mode: str = "memory"

    def compatible(self, other: Failure) -> bool:
        return (
            self.kind == other.kind
            and self.path[:2] == other.path[:2]
            and self.session_mode == other.session_mode
        )


def request_for_state(state: ProjectState) -> dict[str, Any]:
    first_name = "RenamedA.sol" if state.alternate_source_names else "A.sol"
    reexport_name = "RenamedB.sol" if state.alternate_source_names else "B.sol"
    first_type = "bytes32" if state.signature_failure else "uint256"
    first_expression = "uint256(x)" if state.signature_failure else "x"
    first_source = (
        PREAMBLE
        + f"function f({first_type} x) pure returns (uint256) {{\n"
        + f"    return {first_expression} + {state.first_body};\n"
        + "}\n"
        + "function f(address) pure returns (uint256) {\n"
        + f"    return {state.second_body};\n"
        + "}\n"
    )
    if state.selective_reexport:
        reexport_import = f'import {{f as exportF}} from "{first_name}";\n'
        called_symbol = "exportF"
    else:
        reexport_import = f'import "{first_name}";\n'
        called_symbol = "f"
    reexport_source = (
        PREAMBLE
        + reexport_import
        + ('import "Cycle.sol";\n' if state.import_cycle else "")
        + "library ReexportMarker {\n"
        + "    function marker() internal pure returns (uint256) { return 1; }\n"
        + "}\n"
    )
    cycle_source = PREAMBLE
    if state.import_cycle:
        cycle_source += f'import "{reexport_name}";\n'
    cycle_source += "library CycleMarker {}\n"
    auth_source = (
        PREAMBLE
        + "function authorized(uint256 value) pure returns (bool) {\n"
        + "    return value == 7;\n"
        + "}\n"
        + "function rejected(uint256 value) pure returns (bool) {\n"
        + "    return value != 7;\n"
        + "}\n"
    )
    alias_import = (
        "import {authorized as rejected, rejected as authorized} from \"Auth.sol\";\n"
        if state.imported_alias_swap
        else "import {authorized, rejected} from \"Auth.sol\";\n"
    )
    main_source = (
        PREAMBLE
        + f'import * as R from "{reexport_name}";\n'
        + 'import * as M from "Aliases.sol";\n'
        + "using {M.authorized, M.rejected} for uint256;\n"
        + "contract Main {\n"
        + "    function symbol(uint256 x) external pure returns (uint256) {\n"
        + f"        return R.{called_symbol}(x);\n"
        + "    }\n"
        + "    function isAuthorized(uint256 x) external pure returns (bool) {\n"
        + "        return x.authorized();\n"
        + "    }\n"
        + "    function isRejected(uint256 x) external pure returns (bool) {\n"
        + "        return x.rejected();\n"
        + "    }\n"
        + "}\n"
    )
    source_entries = [
        (first_name, first_source),
        (reexport_name, reexport_source),
        ("Cycle.sol", cycle_source),
        ("Auth.sol", auth_source),
        ("Aliases.sol", PREAMBLE + alias_import),
        ("Recursive.sol", PREAMBLE + RECURSIVE_SHAPES[state.recursive_shape] + "\n"),
        ("Main.sol", main_source),
    ]
    if state.reverse_source_order:
        source_entries.reverse()
    if state.extra_source:
        source_entries.append(
            (
                "Extra.sol",
                PREAMBLE
                + "contract Extra {\n"
                + "    function value() external pure returns (uint256) { return 99; }\n"
                + "}\n",
            )
        )
    return {
        "language": "Solidity",
        "sources": {name: {"content": source} for name, source in source_entries},
        "settings": {
            "viaIR": True,
            "optimizer": {"enabled": True, "runs": state.optimizer_runs},
            "metadata": {"appendCBOR": False},
            "outputSelection": {"*": {"*": list(ARTIFACTS[state.projection])}},
        },
    }


def generate_history(rng: random.Random, max_revisions: int) -> list[dict[str, Any]]:
    state = ProjectState()
    history = [request_for_state(state)]
    for _ in range(rng.randint(1, max_revisions - 1)):
        state.mutate(rng)
        history.append(request_for_state(state))
    return history


def seed_histories() -> tuple[list[dict[str, Any]], ...]:
    reexport = ProjectState()
    reexport_history = [request_for_state(reexport)]
    reexport.selective_reexport = True
    reexport_history.append(request_for_state(reexport))
    reexport.first_body = 20
    reexport_history.append(request_for_state(reexport))
    reexport.signature_failure = True
    reexport_history.append(request_for_state(reexport))
    reexport.signature_failure = False
    reexport_history.append(request_for_state(reexport))

    aliases = ProjectState()
    alias_history = [request_for_state(aliases)]
    aliases.imported_alias_swap = True
    alias_history.append(request_for_state(aliases))
    aliases.reverse_source_order = True
    alias_history.append(request_for_state(aliases))

    recursive = ProjectState()
    recursive_history = [request_for_state(recursive)]
    recursive.recursive_shape = 3
    recursive_history.append(request_for_state(recursive))
    recursive.recursive_shape = 4
    recursive_history.append(request_for_state(recursive))
    recursive.recursive_shape = 1
    recursive_history.append(request_for_state(recursive))
    return reexport_history, alias_history, recursive_history


def encode_request(request: dict[str, Any]) -> bytes:
    return json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()


def resolve_executable(value: str) -> Path:
    if os.sep in value or (os.altsep and os.altsep in value):
        result = Path(value).expanduser().resolve()
        if not result.is_file():
            raise RuntimeError(f"executable does not exist: {result}")
        return result
    found = shutil.which(value)
    if found is None:
        raise RuntimeError(f"executable not found on PATH: {value}")
    return Path(found).resolve()


def compiler_version(executable: Path) -> str:
    result = subprocess.run(
        [str(executable), "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError(f"{executable} --version failed: {result.stdout}{result.stderr}")
    return result.stdout.strip()


def evm_environment(executable: Path) -> dict[str, str]:
    environment = dict(os.environ)
    library_directory = executable.parent.parent / "lib"
    if library_directory.is_dir():
        for name in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
            existing = environment.get(name)
            environment[name] = (
                f"{library_directory}{os.pathsep}{existing}"
                if existing
                else str(library_directory)
            )
    return environment


def evm_version(executable: Path) -> str:
    result = subprocess.run(
        [str(executable), "--version"],
        check=False,
        capture_output=True,
        env=evm_environment(executable),
        text=True,
        timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError(f"{executable} --version failed: {result.stdout}{result.stderr}")
    return (result.stdout + result.stderr).strip()


def read_exact(stream: Any, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise EOFError("compiler service closed an incomplete response")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class CandidateService:
    def __init__(
        self,
        executable: Path,
        timeout: float,
        directory: Path,
        session_mode: str,
    ):
        self.timeout = timeout
        self.responses: queue.Queue[bytes | Exception] = queue.Queue()
        self.errors: list[bytes] = []
        environment = dict(os.environ)
        environment["XDG_CACHE_HOME"] = str(directory / "cache")
        environment["XDG_CONFIG_HOME"] = str(directory / "config")
        command = [str(executable), "serve", "--stdio"]
        if session_mode == "memory":
            command.append("--no-cache")
        else:
            config = directory / "config" / "oksolc"
            config.mkdir(parents=True, exist_ok=True)
            (config / "config.toml").write_text("cache = true\n")
        self.process = subprocess.Popen(
            command,
            cwd=directory,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.reader = threading.Thread(target=self._read_responses, daemon=True)
        self.error_reader = threading.Thread(target=self._read_errors, daemon=True)
        self.reader.start()
        self.error_reader.start()

    def _read_responses(self) -> None:
        try:
            assert self.process.stdout is not None
            while True:
                content_length: int | None = None
                while True:
                    line = self.process.stdout.readline()
                    if not line:
                        if content_length is None:
                            return
                        raise EOFError("compiler service closed an incomplete header")
                    if line in (b"\n", b"\r\n"):
                        break
                    name, separator, value = line.partition(b":")
                    if separator and name.strip().lower() == b"content-length":
                        content_length = int(value.strip())
                if content_length is None:
                    raise ValueError("compiler service omitted Content-Length")
                self.responses.put(read_exact(self.process.stdout, content_length))
        except Exception as error:
            self.responses.put(error)

    def _read_errors(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            self.errors.append(line)

    def compile(self, request: dict[str, Any]) -> bytes:
        body = encode_request(request)
        if self.process.poll() is not None:
            raise RuntimeError(self.diagnostic("compiler service exited"))
        assert self.process.stdin is not None
        try:
            self.process.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode())
            self.process.stdin.write(body)
            self.process.stdin.flush()
        except BrokenPipeError as error:
            raise RuntimeError(self.diagnostic("compiler service broke its input pipe")) from error
        try:
            response = self.responses.get(timeout=self.timeout)
        except queue.Empty as error:
            raise TimeoutError(self.diagnostic("compiler service timed out")) from error
        if isinstance(response, Exception):
            raise RuntimeError(self.diagnostic(str(response))) from response
        return response

    def diagnostic(self, message: str) -> str:
        errors = b"".join(self.errors).decode(errors="replace")
        return f"{message}; exit={self.process.poll()}; stderr={errors}"

    def close(self) -> None:
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except BrokenPipeError:
                pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        self.error_reader.join(timeout=2)
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()


def parse_output(label: str, raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} emitted invalid Standard JSON: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} emitted a non-object Standard JSON response")
    return value


def run_compiler(
    executable: Path,
    request: dict[str, Any],
    timeout: float,
    candidate: bool = False,
) -> dict[str, Any]:
    command = [str(executable), "--standard-json"]
    if candidate:
        command.append("--no-cache")
    result = subprocess.run(
        command,
        input=encode_request(request),
        check=False,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(command)} exited {result.returncode}: "
            + result.stderr.decode(errors="replace")
        )
    return parse_output(str(executable), result.stdout)


def first_difference(left: Any, right: Any, path: tuple[str, ...] = ()) -> tuple[str, ...]:
    if type(left) is not type(right):
        return path + ("<type>",)
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            if key not in left or key not in right:
                return path + (str(key),)
            difference = first_difference(left[key], right[key], path + (str(key),))
            if difference:
                return difference
        return ()
    if isinstance(left, list):
        if len(left) != len(right):
            return path + ("<length>",)
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            difference = first_difference(left_item, right_item, path + (str(index),))
            if difference:
                return difference
        return ()
    return () if left == right else path


def semantic_case(
    request: dict[str, Any],
    revision: int,
) -> tuple[str, int, int] | None:
    sources = request.get("sources")
    if not isinstance(sources, dict):
        return None
    first_source = next(
        (
            source.get("content")
            for name, source in sources.items()
            if name in ("A.sol", "RenamedA.sol") and isinstance(source, dict)
        ),
        None,
    )
    aliases = sources.get("Aliases.sol")
    if not isinstance(first_source, str) or not isinstance(aliases, dict):
        return None
    body_match = re.search(r"return x \+ ([0-9]+);", first_source)
    alias_source = aliases.get("content")
    if body_match is None or not isinstance(alias_source, str):
        return None
    first_body = int(body_match.group(1))
    swapped = "authorized as rejected" in alias_source
    digest = hashlib.sha256(encode_request(request) + revision.to_bytes(4, "little")).digest()
    value = (0, 7, 42)[digest[1] % 3]
    signature = (
        "symbol(uint256)",
        "isAuthorized(uint256)",
        "isRejected(uint256)",
    )[digest[0] % 3]
    if signature == "symbol(uint256)":
        expected = value + first_body
    elif signature == "isAuthorized(uint256)":
        expected = int(value != 7 if swapped else value == 7)
    else:
        expected = int(value == 7 if swapped else value != 7)
    return signature, value, expected


def main_artifact(output: dict[str, Any]) -> tuple[str, dict[str, str]] | None:
    try:
        evm = output["contracts"]["Main.sol"]["Main"]["evm"]
        code = evm["deployedBytecode"]["object"]
        methods = evm["methodIdentifiers"]
    except (KeyError, TypeError):
        return None
    if not isinstance(code, str) or not isinstance(methods, dict):
        return None
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in methods.items()):
        return None
    return code, methods


def run_evm(
    executable: Path,
    code: str,
    calldata: str,
    timeout: float,
) -> tuple[str, str]:
    result = subprocess.run(
        [
            str(executable),
            "run",
            "--gas",
            "10000000",
            "--input",
            calldata,
            code,
        ],
        check=False,
        capture_output=True,
        env=evm_environment(executable),
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"evm execution failed with {result.returncode}: {result.stdout}{result.stderr}"
        )
    status = re.search(r"^Result:\s*(\S+)\s*$", result.stdout, re.MULTILINE)
    output = re.search(r"^Output:\s*([0-9a-fA-F]*)\s*$", result.stdout, re.MULTILINE)
    if status is None or output is None:
        raise RuntimeError(f"unrecognized evm output: {result.stdout}{result.stderr}")
    return status.group(1), output.group(1).lower()


def semantic_mismatch(
    executable: Path,
    output: dict[str, Any],
    request: dict[str, Any],
    revision: int,
    timeout: float,
) -> tuple[str, str] | None:
    case = semantic_case(request, revision)
    artifact = main_artifact(output)
    if case is None or artifact is None:
        return None
    signature, value, expected = case
    code, methods = artifact
    selector = methods.get(signature)
    if selector is None:
        return signature, "compiled output omitted its method selector"
    calldata = selector + value.to_bytes(32, "big").hex()
    status, actual = run_evm(executable, code, calldata, timeout)
    expected_output = expected.to_bytes(32, "big").hex()
    if status != "success" or actual != expected_output:
        return (
            signature,
            f"input={value}, expected success/{expected_output}, got {status}/{actual}",
        )
    return None


def evaluate_history(
    candidate: Path,
    reference: Path,
    history: Sequence[dict[str, Any]],
    timeout: float,
    classify: bool = False,
    evm: Path | None = None,
    session_mode: str = "memory",
) -> Failure | None:
    with tempfile.TemporaryDirectory(prefix="oksolc-differential-") as temporary:
        directory = Path(temporary).resolve()
        (directory / ".git").mkdir()
        service = CandidateService(candidate, timeout, directory, session_mode)
        try:
            for revision, request in enumerate(history):
                reference_output = run_compiler(reference, request, timeout)
                try:
                    candidate_raw = service.compile(request)
                    candidate_output = parse_output("stateful oksolc", candidate_raw)
                except TimeoutError as error:
                    return Failure(
                        "candidate-timeout",
                        revision,
                        (),
                        str(error),
                        reference_output,
                        session_mode=session_mode,
                    )
                except (RuntimeError, ValueError) as error:
                    return Failure(
                        "candidate-service-failure",
                        revision,
                        (),
                        str(error),
                        reference_output,
                        session_mode=session_mode,
                    )
                if candidate_output != reference_output:
                    path = first_difference(reference_output, candidate_output)
                    failure = Failure(
                        "output-mismatch",
                        revision,
                        path,
                        "stateful output differs from solc 0.8.36",
                        reference_output,
                        candidate_output,
                        session_mode=session_mode,
                    )
                    if classify:
                        try:
                            failure.candidate_fresh = run_compiler(
                                candidate,
                                request,
                                timeout,
                                candidate=True,
                            )
                            failure.detail = (
                                "incremental output differs; clean oksolc matches solc"
                                if failure.candidate_fresh == reference_output
                                else "clean and incremental oksolc differ from solc"
                            )
                        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
                            failure.detail += f"; clean classification failed: {error}"
                    return failure
                if evm is not None:
                    mismatch = semantic_mismatch(
                        evm,
                        candidate_output,
                        request,
                        revision,
                        timeout,
                    )
                    if mismatch is not None:
                        signature, detail = mismatch
                        return Failure(
                            "semantic-mismatch",
                            revision,
                            ("execution", signature),
                            detail,
                            reference_output,
                            candidate_output,
                            session_mode=session_mode,
                        )
                if session_mode == "restart" and revision + 1 != len(history):
                    service.close()
                    service = CandidateService(candidate, timeout, directory, session_mode)
        finally:
            service.close()
    return None


def ddmin_history(
    history: list[dict[str, Any]],
    preserves: Callable[[list[dict[str, Any]]], bool],
) -> list[dict[str, Any]]:
    if len(history) <= 1:
        return history
    prefix = history[:-1]
    final = history[-1]
    granularity = 2
    while prefix:
        chunk_size = max(1, (len(prefix) + granularity - 1) // granularity)
        reduced = False
        for start in range(0, len(prefix), chunk_size):
            candidate = prefix[:start] + prefix[start + chunk_size :] + [final]
            if preserves(candidate):
                prefix = candidate[:-1]
                granularity = max(2, granularity - 1)
                reduced = True
                break
        if reduced:
            continue
        if granularity >= len(prefix):
            break
        granularity = min(len(prefix), granularity * 2)
    return prefix + [final]


def remove_source(
    history: list[dict[str, Any]],
    source_name: str,
) -> list[dict[str, Any]]:
    candidate = deepcopy(history)
    for request in candidate:
        request.get("sources", {}).pop(source_name, None)
    return candidate


def remove_source_line(
    history: list[dict[str, Any]],
    source_name: str,
    line_index: int,
) -> list[dict[str, Any]]:
    candidate = deepcopy(history)
    for request in candidate:
        source = request.get("sources", {}).get(source_name)
        if not isinstance(source, dict) or not isinstance(source.get("content"), str):
            continue
        lines = source["content"].splitlines(keepends=True)
        if line_index < len(lines):
            del lines[line_index]
            source["content"] = "".join(lines)
    return candidate


def reduce_history(
    candidate: Path,
    reference: Path,
    history: list[dict[str, Any]],
    original: Failure,
    timeout: float,
    evm: Path | None = None,
    session_mode: str = "memory",
) -> tuple[list[dict[str, Any]], int]:
    evaluations = 0

    def preserves(value: list[dict[str, Any]]) -> bool:
        nonlocal evaluations
        evaluations += 1
        failure = evaluate_history(
            candidate,
            reference,
            value,
            timeout,
            evm=evm,
            session_mode=session_mode,
        )
        return failure is not None and original.compatible(failure)

    result = ddmin_history(history[: original.revision + 1], preserves)
    source_names = sorted(
        {name for request in result for name in request.get("sources", {})}
    )
    for source_name in source_names:
        reduced = remove_source(result, source_name)
        if preserves(reduced):
            result = reduced

    for source_name in source_names:
        line_index = 0
        while True:
            line_counts = [
                len(source["content"].splitlines(keepends=True))
                for request in result
                if isinstance((source := request.get("sources", {}).get(source_name)), dict)
                and isinstance(source.get("content"), str)
            ]
            if not line_counts or line_index >= max(line_counts):
                break
            reduced = remove_source_line(result, source_name, line_index)
            if preserves(reduced):
                result = reduced
            else:
                line_index += 1
    return result, evaluations


def failure_json(failure: Failure | None) -> Any:
    if failure is None:
        return None
    result = asdict(failure)
    result["path"] = list(failure.path)
    return result


def write_bundle(
    path: Path,
    history: Sequence[dict[str, Any]],
    failure: Failure,
    candidate_version: str,
    reference_version: str,
    seed: int,
    iteration: int,
    reducer_evaluations: int = 0,
    execution_oracle_version: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "oksolc-differential-reproducer-v1",
                "seed": seed,
                "iteration": iteration,
                "candidate_version": candidate_version,
                "reference_version": reference_version,
                "execution_oracle_version": execution_oracle_version,
                "reducer_evaluations": reducer_evaluations,
                "failure": failure_json(failure),
                "history": history,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def parse_seed(value: str) -> int:
    return int(value, 0)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, help="oksolc executable")
    parser.add_argument("--reference", required=True, help="solc 0.8.36 executable")
    parser.add_argument("--evm", help="optional evmone executable for semantic execution")
    parser.add_argument(
        "--session-mode",
        choices=("memory", "persistent", "restart", "all"),
        default="memory",
        help="candidate cache/restart mode",
    )
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--max-revisions", type=int, default=6)
    parser.add_argument("--seed", type=parse_seed, default=0x6A09E667F3BCC909)
    parser.add_argument("--timeout", type=float, default=40.0)
    parser.add_argument("--artifacts", type=Path, default=Path("build/fuzz-differential"))
    parser.add_argument("--replay", type=Path)
    parser.add_argument("--no-reduce", action="store_true")
    return parser.parse_args()


def main() -> int:
    options = parse_arguments()
    if options.iterations <= 0:
        raise RuntimeError("--iterations must be positive")
    if options.max_revisions < 2:
        raise RuntimeError("--max-revisions must be at least two")
    if options.timeout <= 0:
        raise RuntimeError("--timeout must be positive")
    candidate = resolve_executable(options.candidate)
    reference = resolve_executable(options.reference)
    evm = resolve_executable(options.evm) if options.evm else None
    candidate_version = compiler_version(candidate)
    reference_version = compiler_version(reference)
    if "Version: 0.8.36" not in reference_version:
        raise RuntimeError(f"reference compiler is not solc 0.8.36: {reference_version}")
    execution_oracle_version = evm_version(evm) if evm is not None else None

    if options.replay is not None:
        bundle = json.loads(options.replay.read_text())
        history = bundle["history"]
        replay_mode = bundle.get("failure", {}).get("session_mode", options.session_mode)
        if replay_mode == "all":
            replay_mode = "memory"
        failure = evaluate_history(
            candidate,
            reference,
            history,
            options.timeout,
            classify=True,
            evm=evm,
            session_mode=replay_mode,
        )
        if failure is None:
            print(f"reproducer no longer fails: {options.replay}")
            return 0
        print(
            f"reproduced {failure.kind} at revision {failure.revision}: "
            f"{failure.detail}; path={'/'.join(failure.path)}"
        )
        return 1

    rng = random.Random(options.seed)
    seeds = seed_histories()
    session_modes = (
        ("memory", "persistent", "restart")
        if options.session_mode == "all"
        else (options.session_mode,)
    )
    for iteration in range(options.iterations):
        history = (
            seeds[iteration]
            if iteration < len(seeds)
            else generate_history(rng, options.max_revisions)
        )
        failure = None
        for session_mode in session_modes:
            failure = evaluate_history(
                candidate,
                reference,
                history,
                options.timeout,
                classify=True,
                evm=evm,
                session_mode=session_mode,
            )
            if failure is not None:
                break
        if failure is None:
            continue
        stem = f"failure-{options.seed:016x}-{iteration:06d}"
        original_path = options.artifacts / f"{stem}-original.json"
        write_bundle(
            original_path,
            history[: failure.revision + 1],
            failure,
            candidate_version,
            reference_version,
            options.seed,
            iteration,
            execution_oracle_version=execution_oracle_version,
        )
        minimized = history[: failure.revision + 1]
        evaluations = 0
        if not options.no_reduce:
            minimized, evaluations = reduce_history(
                candidate,
                reference,
                minimized,
                failure,
                options.timeout,
                evm,
                failure.session_mode,
            )
        minimized_failure = evaluate_history(
            candidate,
            reference,
            minimized,
            options.timeout,
            classify=True,
            evm=evm,
            session_mode=failure.session_mode,
        )
        if minimized_failure is None:
            raise RuntimeError("reducer lost the original failure")
        minimized_path = options.artifacts / f"{stem}-minimized.json"
        write_bundle(
            minimized_path,
            minimized,
            minimized_failure,
            candidate_version,
            reference_version,
            options.seed,
            iteration,
            evaluations,
            execution_oracle_version,
        )
        print(
            f"found {failure.kind} at iteration {iteration}, revision {failure.revision}; "
            f"saved {original_path} and {minimized_path}",
            file=sys.stderr,
        )
        return 1
    print(
        f"stateful differential fuzzing passed: {options.iterations} histories, "
        f"seed {options.seed:#x}, up to {options.max_revisions} revisions"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"compiler differential fuzzer: {error}", file=sys.stderr)
        raise SystemExit(2) from error
