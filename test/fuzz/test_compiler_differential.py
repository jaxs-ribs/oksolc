#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0
# Copyright (C) 2026 OKcontract Pte. Ltd.

import importlib.util
import json
from pathlib import Path
import random
import sys
import tempfile
import time
import unittest
from unittest import mock


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
            wide_base_storage=True,
            inherited_signature_failure=True,
            super_signature_failure=True,
            error_signature_failure=True,
        )
        request = differential.request_for_state(state)
        sources = request["sources"]
        self.assertIn("RenamedA.sol", sources)
        self.assertIn("RenamedB.sol", sources)
        self.assertIn("f as exportF", sources["RenamedB.sol"]["content"])
        self.assertIn('import "RenamedB.sol"', sources["Cycle.sol"]["content"])
        self.assertIn("authorized as rejected", sources["Aliases.sol"]["content"])
        self.assertIn("struct B { B parent; }", sources["Recursive.sol"]["content"])
        self.assertIn("uint256 internal left", sources["Dependency.sol"]["content"])
        self.assertIn("function inherited(bytes32", sources["Dependency.sol"]["content"])
        self.assertIn("error SharedError(bytes32", sources["Dependency.sol"]["content"])
        self.assertIn("function dispatch(uint64", sources["Hierarchy.sol"]["content"])

    def test_seed_histories_cover_edits_rejection_and_recovery(self):
        (
            reexports,
            aliases,
            recursive,
            dependencies,
            hierarchy,
            backend,
            diagnostics,
            resolution,
            push,
            yul_ast,
            static_library,
            inner_cycle,
            storage_boundary,
            conversion_boundary,
            fixed_point_boundary,
        ) = differential.seed_histories()
        self.assertEqual(len(reexports), 5)
        self.assertIn("bytes32", reexports[-2]["sources"]["A.sol"]["content"])
        self.assertNotIn("bytes32", reexports[-1]["sources"]["A.sol"]["content"])
        self.assertIn("authorized as rejected", aliases[-1]["sources"]["Aliases.sol"]["content"])
        self.assertIn("struct B { B parent; }", recursive[-2]["sources"]["Recursive.sol"]["content"])
        self.assertEqual(len(dependencies), 9)
        self.assertIn("return value + 9", dependencies[1]["sources"]["Dependency.sol"]["content"])
        self.assertIn("uint256 internal left", dependencies[4]["sources"]["Dependency.sol"]["content"])
        self.assertIn("function inherited(bytes32", dependencies[5]["sources"]["Dependency.sol"]["content"])
        self.assertNotIn("function inherited(bytes32", dependencies[6]["sources"]["Dependency.sol"]["content"])
        self.assertIn("error SharedError(bytes32", dependencies[7]["sources"]["Dependency.sol"]["content"])
        self.assertNotIn("error SharedError(bytes32", dependencies[8]["sources"]["Dependency.sol"]["content"])
        self.assertEqual(len(hierarchy), 3)
        self.assertIn("function dispatch(uint64", hierarchy[1]["sources"]["Hierarchy.sol"]["content"])
        self.assertIn("function dispatch(uint256", hierarchy[2]["sources"]["Hierarchy.sol"]["content"])
        self.assertEqual(backend[1]["settings"]["evmVersion"], "homestead")
        self.assertFalse(backend[1]["settings"]["optimizer"]["enabled"])
        self.assertIn("new uint256[](2)", diagnostics[1]["sources"]["Diagnostics.sol"]["content"])
        self.assertIn("values[2]", diagnostics[1]["sources"]["Diagnostics.sol"]["content"])
        self.assertIn("missingFirst();", resolution[1]["sources"]["Resolution.sol"]["content"])
        self.assertNotIn("missingFirst();", resolution[2]["sources"]["Resolution.sol"]["content"])
        self.assertIn("missingSecond();", resolution[2]["sources"]["Resolution.sol"]["content"])
        self.assertIn("uint8[]", push[1]["sources"]["Push.sol"]["content"])
        self.assertIn("push(256)", push[1]["sources"]["Push.sol"]["content"])
        self.assertIn(
            "115792089237316195423570985008687907853269984665640564039457584007913129639936",
            yul_ast[1]["sources"]["Assembly.sol"]["content"],
        )
        self.assertIn("uint32 value", static_library[1]["sources"]["Library.sol"]["content"])
        self.assertIn("L.f(value)", static_library[1]["sources"]["Consumer.sol"]["content"])
        self.assertIn("B[0]", inner_cycle[1]["sources"]["Cycle.sol"]["content"])
        self.assertIn("B parent", inner_cycle[2]["sources"]["Cycle.sol"]["content"])
        self.assertIn(
            "57896044618658097711785492504343953926634992332820282019728792003956564819968",
            storage_boundary[1]["sources"]["StorageBoundary.sol"]["content"],
        )
        self.assertIn(
            'bytes1("root")',
            conversion_boundary[1]["sources"]["ConversionBoundary.sol"]["content"],
        )
        self.assertIn(
            'string(hex"ff")',
            conversion_boundary[2]["sources"]["ConversionBoundary.sol"]["content"],
        )
        self.assertIn(
            "fixed128x10 converted",
            fixed_point_boundary[1]["sources"]["FixedPointBoundary.sol"]["content"],
        )
        self.assertIn(
            "ufixed128x10 converted",
            fixed_point_boundary[2]["sources"]["FixedPointBoundary.sol"]["content"],
        )

    def test_targeted_histories_are_deterministic_and_stress_boundaries(self):
        first = differential.generate_targeted_history(random.Random(11), 8)
        second = differential.generate_targeted_history(random.Random(11), 8)
        self.assertEqual(first, second)
        self.assertGreaterEqual(len(first), 2)
        self.assertLessEqual(len(first), 8)
        source_name = next(iter(first[0]["sources"]))
        self.assertTrue(
            source_name
            in (
                "StorageBoundary.sol",
                "ConversionBoundary.sol",
                "FixedPointBoundary.sol",
            )
        )
        self.assertTrue(any(revision != first[0] for revision in first[1:]))

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
        later_member = differential.Failure(
            "output-mismatch",
            0,
            ("errors", "<length>"),
            "",
            {"errors": [
                {"severity": "error", "errorCode": "2353"},
                {"severity": "error", "errorCode": "9582"},
            ]},
            diagnostic("2353"),
        )
        different_later_error = differential.Failure(
            "output-mismatch",
            0,
            ("errors", "<length>"),
            "",
            {"errors": [
                {"severity": "error", "errorCode": "2353"},
                {"severity": "error", "errorCode": "6160"},
            ]},
            diagnostic("2353"),
        )
        self.assertFalse(later_member.compatible(different_later_error))

        first_uncoded = differential.Failure(
            "output-mismatch",
            0,
            ("contracts",),
            "",
            {},
            {"errors": [{"severity": "error", "type": "YulException", "message": "first"}]},
        )
        second_uncoded = differential.Failure(
            "output-mismatch",
            0,
            ("contracts",),
            "",
            {},
            {"errors": [{"severity": "error", "type": "YulException", "message": "second"}]},
        )
        self.assertNotEqual(
            first_uncoded.campaign_signature(),
            second_uncoded.campaign_signature(),
        )

    def test_reference_internal_errors_fall_back_to_clean_candidate(self):
        reference = Path("/reference")
        candidate = Path("/candidate")
        internal_error = {
            "errors": [
                {
                    "severity": "error",
                    "type": "InternalCompilerError",
                    "message": "upstream failed",
                }
            ]
        }
        clean = {"sources": {"C.sol": {"id": 0}}}

        def run_compiler(executable, _request, _timeout, candidate=False):
            if executable == reference:
                return internal_error
            self.assertTrue(candidate)
            return clean

        class CandidateService:
            def __init__(self, *_args):
                pass

            def compile(self, _request):
                return json.dumps(clean).encode()

            def close(self):
                pass

        self.assertTrue(differential.has_internal_compiler_error(internal_error))
        with (
            mock.patch.object(differential, "run_compiler", run_compiler),
            mock.patch.object(differential, "CandidateService", CandidateService),
        ):
            self.assertIsNone(
                differential.evaluate_history(
                    candidate,
                    reference,
                    [{"language": "Solidity"}],
                    1,
                )
            )

    def test_campaign_signatures_bucket_the_divergent_diagnostic(self):
        def diagnostic(code):
            return {"severity": "error", "errorCode": code}

        first = differential.Failure(
            "output-mismatch",
            0,
            ("errors", "0", "formattedMessage"),
            "",
            {"errors": [diagnostic("6359")]},
            {"errors": [diagnostic("6359")]},
        )
        later = differential.Failure(
            "output-mismatch",
            0,
            ("errors", "2", "formattedMessage"),
            "",
            {"errors": [diagnostic("9098"), diagnostic("4822"), diagnostic("6359")]},
            {"errors": [diagnostic("9098"), diagnostic("4822"), diagnostic("6359")]},
        )
        other = differential.Failure(
            "output-mismatch",
            0,
            ("errors", "0", "formattedMessage"),
            "",
            {"errors": [diagnostic("5992")]},
            {"errors": [diagnostic("5992")]},
        )
        self.assertEqual(first.campaign_signature(), later.campaign_signature())
        self.assertNotEqual(first.campaign_signature(), other.campaign_signature())

        missing_7576 = differential.Failure(
            "output-mismatch",
            0,
            ("errors", "<length>"),
            "",
            {"errors": [diagnostic("7576")]},
            {"errors": [diagnostic("7576"), diagnostic("7576")]},
        )
        missing_9582 = differential.Failure(
            "output-mismatch",
            0,
            ("errors", "<length>"),
            "",
            {"errors": [diagnostic("9582")]},
            {"errors": [diagnostic("9553")]},
        )
        missing_9582_with_cascade = differential.Failure(
            "output-mismatch",
            0,
            ("errors", "<length>"),
            "",
            {"errors": [diagnostic("9582")]},
            {"errors": [diagnostic("9553"), diagnostic("7407")]},
        )
        self.assertEqual(
            missing_9582.campaign_signature(),
            missing_9582_with_cascade.campaign_signature(),
        )
        self.assertNotEqual(
            missing_7576.campaign_signature(),
            missing_9582.campaign_signature(),
        )

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

    def test_optimizer_mutation_toggles_classic_stack_lowering(self):
        class OptimizerMutation:
            @staticmethod
            def randrange(_stop):
                return len(differential.SOURCE_MUTATIONS)

            @staticmethod
            def choice(values):
                return values[0]

        request = {
            "language": "Solidity",
            "sources": {"C.sol": {"content": "contract C {}"}},
            "settings": {"optimizer": {"enabled": True, "runs": 200}},
        }
        mutated = differential.mutate_corpus_request(request, OptimizerMutation())
        self.assertTrue(request["settings"]["optimizer"]["enabled"])
        self.assertFalse(mutated["settings"]["optimizer"]["enabled"])

    def test_source_mutations_cover_type_location_and_operator_boundaries(self):
        source = (
            "contract C { function f(int32 x, bytes4 data, address payable target, "
            "uint256[2] memory values) public payable returns (bool) { "
            'hex"ff"; return true && x + 1 <= 2 != (values.length == 0); } }'
        )
        for pattern, _ in differential.SOURCE_MUTATIONS:
            self.assertIsNotNone(pattern.search(source), pattern.pattern)


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


class CandidateServiceTests(unittest.TestCase):
    def test_closed_worker_is_reported_without_waiting_for_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worker = root / "worker.py"
            worker.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "sys.stdin.buffer.read(1)\n"
                "raise SystemExit(1)\n"
            )
            worker.chmod(0o755)
            service = differential.CandidateService(worker, 5, root, "memory")
            started = time.monotonic()
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "compiler service closed before response",
                ):
                    service.compile({"language": "Solidity"})
            finally:
                service.close()
            self.assertLess(time.monotonic() - started, 2)


if __name__ == "__main__":
    unittest.main()
