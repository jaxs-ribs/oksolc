#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0
# Copyright (C) 2026 OKcontract Pte. Ltd.

import importlib.util
import json
from pathlib import Path
import random
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("compiler_differential.py")
SPEC = importlib.util.spec_from_file_location("compiler_differential", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
differential = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = differential
SPEC.loader.exec_module(differential)


class GeneratorTests(unittest.TestCase):
    def test_history_is_deterministic_and_requests_codegen_artifacts(self):
        first = differential.generate_history(random.Random(41), 6)
        second = differential.generate_history(random.Random(41), 6)
        self.assertEqual(first, second)
        self.assertGreaterEqual(len(first), 2)
        for request in first:
            self.assertEqual(request["language"], "Solidity")
            self.assertIn("Main.sol", request["sources"])
            self.assertTrue(request["settings"]["viaIR"])
            artifacts = request["settings"]["outputSelection"]["*"]["*"]
            self.assertTrue(any(value.startswith("evm.") for value in artifacts))

    def test_generator_reaches_reported_feature_combinations(self):
        state = differential.ProjectState(
            signature_failure=True,
            selective_reexport=True,
            import_cycle=True,
            imported_alias_swap=True,
            recursive_shape=4,
            alternate_source_names=True,
            reverse_source_order=True,
        )
        request = differential.request_for_state(state)
        sources = request["sources"]
        self.assertIn("RenamedA.sol", sources)
        self.assertIn("RenamedB.sol", sources)
        self.assertIn("f as exportF", sources["RenamedB.sol"]["content"])
        self.assertIn('import "RenamedB.sol"', sources["Cycle.sol"]["content"])
        self.assertIn("authorized as rejected", sources["Aliases.sol"]["content"])
        self.assertIn("struct B { B parent; }", sources["Recursive.sol"]["content"])

    def test_seed_histories_cover_edits_rejection_and_recovery(self):
        reexports, aliases, recursive = differential.seed_histories()
        self.assertEqual(len(reexports), 5)
        self.assertIn("bytes32", reexports[-2]["sources"]["A.sol"]["content"])
        self.assertNotIn("bytes32", reexports[-1]["sources"]["A.sol"]["content"])
        self.assertIn("authorized as rejected", aliases[-1]["sources"]["Aliases.sol"]["content"])
        self.assertIn("struct B { B parent; }", recursive[-2]["sources"]["Recursive.sol"]["content"])

    def test_semantic_oracle_derives_expected_result_from_sources(self):
        state = differential.ProjectState(first_body=9, imported_alias_swap=True)
        request = differential.request_for_state(state)
        cases = [differential.semantic_case(request, revision) for revision in range(32)]
        by_signature = {case[0]: case for case in cases if case is not None}
        symbol = by_signature["symbol(uint256)"]
        self.assertEqual(symbol[2], symbol[1] + 9)
        authorized = by_signature["isAuthorized(uint256)"]
        self.assertEqual(authorized[2], int(authorized[1] != 7))
        rejected = by_signature["isRejected(uint256)"]
        self.assertEqual(rejected[2], int(rejected[1] == 7))

    def test_transition_corpus_is_shuffled_batched_and_restored(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            requests = []
            for index in range(5):
                request = {
                    "language": "Solidity",
                    "sources": {f"{index}.sol": {"content": f"contract C{index} {{}}"}},
                }
                requests.append(request)
                (root / f"{index}.json").write_text(json.dumps(request))
            (root / "corpus.json").write_text(json.dumps({"schema": 1}))
            (root / "external.json").write_text(
                json.dumps(
                    {
                        "language": "Solidity",
                        "sources": {
                            "External.sol": {
                                "content": 'import "Missing.sol"; contract External {}'
                            }
                        },
                    }
                )
            )

            histories = differential.corpus_transition_histories(
                root,
                random.Random(7),
                3,
            )
            self.assertEqual([len(history) for history in histories], [3, 3, 2])
            flattened = [request for history in histories for request in history[:-1]]
            self.assertCountEqual(flattened, requests)
            for history in histories:
                self.assertEqual(history[0], history[-1])
                self.assertNotEqual(
                    differential.encode_request(history[0]),
                    differential.encode_request(history[-1]),
                )

    def test_reference_audit_normalizes_ir_and_tracks_oracle_identity(self):
        request = {
            "settings": {"outputSelection": {"*": {"*": ["abi", "ir"]}}}
        }
        self.assertTrue(differential.requests_ir_text(request))
        self.assertFalse(differential.requests_ir_text({"settings": {}}))
        self.assertEqual(
            differential.without_ir_text(
                {
                    "contracts": {
                        "C": {"ir": "pretty", "irOptimized": "compact", "abi": []}
                    }
                }
            ),
            {"contracts": {"C": {"abi": []}}},
        )
        reference = differential.Failure("output-mismatch", 0, (), "", oracle="reference")
        clean = differential.Failure("output-mismatch", 0, (), "", oracle="clean")
        self.assertFalse(reference.compatible(clean))

        def diagnostic(code):
            return {"errors": [{"severity": "error", "errorCode": code}]}

        conversion = differential.Failure(
            "output-mismatch",
            0,
            ("errors", "<length>"),
            "",
            diagnostic("9553"),
            diagnostic("9582"),
        )
        unrelated = differential.Failure(
            "output-mismatch",
            0,
            ("errors", "<length>"),
            "",
            diagnostic("6160"),
            diagnostic("9582"),
        )
        self.assertFalse(conversion.compatible(unrelated))

    def test_corpus_mutations_are_deterministic_and_leave_seed_immutable(self):
        request = {
            "language": "Solidity",
            "sources": {
                "C.sol": {
                    "content": "contract C { function f(uint256 x) external pure returns (bool) { return x == 7; } }"
                }
            },
            "settings": {"optimizer": {"enabled": True, "runs": 200}},
        }
        entries = [("case.json", request)]
        first = differential.generate_corpus_mutation_history(entries, random.Random(17), 6)
        second = differential.generate_corpus_mutation_history(entries, random.Random(17), 6)
        self.assertEqual(first, second)
        self.assertGreaterEqual(len(first), 2)
        self.assertEqual(request["sources"]["C.sol"]["content"].count("uint256"), 1)
        self.assertTrue(any(revision != first[0] for revision in first[1:]))


class ReducerTests(unittest.TestCase):
    def test_history_reducer_preserves_required_ordered_revisions(self):
        history = [{"revision": value} for value in "abcde"]

        def preserves(candidate):
            values = "".join(item["revision"] for item in candidate)
            return "b" in values and "d" in values and values.endswith("e")

        self.assertEqual(
            differential.ddmin_history(history, preserves),
            [{"revision": "b"}, {"revision": "d"}, {"revision": "e"}],
        )

    def test_source_and_line_reduction_leave_inputs_immutable(self):
        history = [{"sources": {"A.sol": {"content": "one\ntwo\n"}}}]
        without_line = differential.remove_source_line(history, "A.sol", 0)
        without_source = differential.remove_source(history, "A.sol")
        self.assertEqual(history[0]["sources"]["A.sol"]["content"], "one\ntwo\n")
        self.assertEqual(without_line[0]["sources"]["A.sol"]["content"], "two\n")
        self.assertEqual(without_source[0]["sources"], {})

    def test_difference_path_identifies_nested_artifact(self):
        left = {"contracts": {"A.sol": {"A": {"evm": {"bytecode": "00"}}}}}
        right = {"contracts": {"A.sol": {"A": {"evm": {"bytecode": "01"}}}}}
        self.assertEqual(
            differential.first_difference(left, right),
            ("contracts", "A.sol", "A", "evm", "bytecode"),
        )


if __name__ == "__main__":
    unittest.main()
