// SPDX-License-Identifier: GPL-3.0
// Copyright (C) 2026 OKcontract Pte. Ltd.

const package = @import("package_options");
const identity = @embedFile("compiler_identity");

pub const oksolc_version = package.oksolc_version;
pub const license_text = package.license_text;
pub const third_party_licenses = package.third_party_licenses;
pub const compiler_build_identity_available = identity.len != 0;
pub const compiler_build_identity: []const u8 = if (compiler_build_identity_available) identity else "unidentified";
