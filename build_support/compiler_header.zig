// SPDX-License-Identifier: GPL-3.0
// Copyright (C) 2026 OKcontract Pte. Ltd.

const std = @import("std");
const Translator = @import("translate_c").Translator;

/// Capture the compiler identity before translation reads working-tree headers.
pub fn translate(dependency: *std.Build.Dependency, identity: std.Build.LazyPath, options: Translator.Options) Translator {
    const translated: Translator = .init(dependency, options);
    identity.addStepDependencies(&translated.run.step);
    return translated;
}
