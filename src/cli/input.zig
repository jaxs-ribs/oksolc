// SPDX-License-Identifier: GPL-3.0
// Copyright (C) 2026 OKcontract Pte. Ltd.

//! Source CLI requests use the Standard JSON settings validator. This module
//! assembles inputs for ordinary compilation.
const std = @import("std");
const JSON = @import("solidity").libsolutil.json;

pub const Options = struct {
    ast: bool = false,
    /// Borrowed remappings.txt contents. Solidity validates each nonempty line.
    remappings: ?[]const u8 = null,
};

pub fn readRemappingsAlloc(allocator: std.mem.Allocator, io: std.Io, project_root: []const u8) !?[]u8 {
    var directory = try openRemappingsRoot(io, project_root);
    defer directory.close(io);
    return readRemappingsAtAlloc(allocator, io, directory);
}

/// Borrows the project's pinned directory capability, including across watch
/// updates. Only an absent remappings.txt is optional; unsafe nodes fail closed.
pub fn readRemappingsAtAlloc(allocator: std.mem.Allocator, io: std.Io, directory: std.Io.Dir) !?[]u8 {
    const path_stat = directory.statFile(io, "remappings.txt", .{ .follow_symlinks = false }) catch |err| switch (err) {
        error.FileNotFound => return null,
        else => return err,
    };
    if (path_stat.kind != .file) return error.RemappingsNotRegularFile;
    var file = try @import("project_sources.zig").openSourceFileNonblocking(directory, io, "remappings.txt");
    defer file.close(io);
    const stat = try file.stat(io);
    if (stat.kind != .file or stat.inode != path_stat.inode) return error.RemappingsNotRegularFile;
    if (stat.size > 1024 * 1024) return error.StreamTooLong;
    var buffer: [16 * 1024]u8 = undefined;
    var reader = file.reader(io, &buffer);
    return try reader.interface.allocRemaining(allocator, .limited(1024 * 1024));
}

fn openRemappingsRoot(io: std.Io, path: []const u8) !std.Io.Dir {
    const root = std.Io.Dir.path.parsePath(path).root;
    var directory = try std.Io.Dir.cwd().openDir(io, if (root.len == 0) "." else root, .{
        .follow_symlinks = false,
    });
    errdefer directory.close(io);
    var components = std.mem.tokenizeAny(u8, path[root.len..], if (@import("builtin").target.os.tag == .windows) "/\\" else "/");
    while (components.next()) |component| {
        const next = try directory.openDir(io, component, .{ .follow_symlinks = false });
        directory.close(io);
        directory = next;
    }
    return directory;
}

pub const RemappingLines = struct {
    lines: std.mem.SplitIterator(u8, .scalar),

    pub fn init(contents: []const u8) RemappingLines {
        return .{ .lines = std.mem.splitScalar(u8, contents, '\n') };
    }

    pub fn next(self: *RemappingLines) ?[]const u8 {
        while (self.lines.next()) |line| {
            const text = std.mem.trim(u8, line, " \t\r");
            if (text.len != 0) return text;
        }
        return null;
    }
};

pub const Source = struct {
    name: []const u8,
    content: []const u8,
};

pub const LoadedSources = struct {
    allocator: std.mem.Allocator,
    sources: []Source,
    owned_names: bool = false,

    pub fn deinit(self: *LoadedSources) void {
        for (self.sources) |source| {
            self.allocator.free(source.content);
            if (self.owned_names) self.allocator.free(source.name);
        }
        self.allocator.free(self.sources);
    }
};

pub fn loadSources(allocator: std.mem.Allocator, io: std.Io, paths: []const []const u8) !LoadedSources {
    return loadSourcesAt(allocator, io, paths, null);
}

/// Source names use the canonical project namespace, including when the
/// command is invoked from a nested directory or receives absolute paths.
pub fn loadSourcesAt(allocator: std.mem.Allocator, io: std.Io, paths: []const []const u8, project: ?[]const u8) !LoadedSources {
    const sources = try allocator.alloc(Source, paths.len);
    errdefer allocator.free(sources);
    var loaded: usize = 0;
    errdefer for (sources[0..loaded]) |source| {
        allocator.free(source.content);
        if (project != null) allocator.free(source.name);
    };
    var total: usize = 0;
    for (paths, sources) |path, *source| {
        var file = try openSourceFile(io, path);
        defer file.close(io);
        var buffer: [16 * 1024]u8 = undefined;
        var reader = file.reader(io, &buffer);
        const content = try reader.interface.allocRemaining(allocator, .limited(64 * 1024 * 1024 -| total));
        errdefer allocator.free(content);
        const name = if (project) |root| name: {
            const canonical = try std.Io.Dir.cwd().realPathFileAlloc(io, path, allocator);
            defer allocator.free(canonical);
            if (!@import("project_sources.zig").pathContainedBy(root, canonical) or canonical.len <= root.len)
                break :name try allocator.dupe(u8, path);
            const relative = try allocator.dupe(u8, std.mem.trimStart(u8, canonical[root.len..], "/\\"));
            std.mem.replaceScalar(u8, relative, '\\', '/');
            break :name relative;
        } else path;
        source.* = .{ .name = name, .content = content };
        loaded += 1;
        total += content.len;
    }
    return .{ .allocator = allocator, .sources = sources, .owned_names = project != null };
}

fn openSourceFile(io: std.Io, path: []const u8) !std.Io.File {
    const root = std.Io.Dir.path.parsePath(path).root;
    var directory = try std.Io.Dir.cwd().openDir(io, if (root.len == 0) "." else root, .{
        .follow_symlinks = false,
    });
    defer directory.close(io);
    var components = std.mem.tokenizeAny(u8, path[root.len..], if (@import("builtin").target.os.tag == .windows) "/\\" else "/");
    while (components.next()) |component| {
        if (components.peek() != null) {
            const next = try directory.openDir(io, component, .{ .follow_symlinks = false });
            directory.close(io);
            directory = next;
            continue;
        }
        const path_stat = try directory.statFile(io, component, .{ .follow_symlinks = false });
        if (path_stat.kind != .file) return error.SourceNotRegularFile;
        var file = try @import("project_sources.zig").openSourceFileNonblocking(directory, io, component);
        errdefer file.close(io);
        const stat = try file.stat(io);
        if (stat.kind != .file or stat.inode != path_stat.inode) return error.SourceNotRegularFile;
        return file;
    }
    return error.SourceNotRegularFile;
}

pub fn buildAlloc(
    allocator: std.mem.Allocator,
    sources: []const Source,
    options: Options,
) ![]u8 {
    const ast = options.ast;
    const remapping_text = options.remappings;
    var arena_state = std.heap.ArenaAllocator.init(allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();

    var root: std.json.Value = .{ .object = .empty };
    try root.object.put(arena, "language", .{ .string = "Solidity" });

    var source_object: std.json.Value = .{ .object = .empty };
    for (sources) |source| {
        if (source_object.object.contains(source.name)) return error.DuplicateSource;
        var entry: std.json.Value = .{ .object = .empty };
        try entry.object.put(arena, "content", .{ .string = source.content });
        try source_object.object.put(arena, source.name, entry);
    }
    try root.object.put(arena, "sources", source_object);

    var optimizer: std.json.Value = .{ .object = .empty };
    try optimizer.object.put(arena, "enabled", .{ .bool = true });
    try optimizer.object.put(arena, "runs", .{ .integer = 200 });

    var artifacts = std.json.Array.init(arena);
    for ([_][]const u8{
        "abi",
        "evm.bytecode.object",
        "evm.deployedBytecode.object",
        "metadata",
    }) |artifact|
        try artifacts.append(.{ .string = artifact });
    // Source inspection needs compiler-owned identities and documentation.
    // Keep the small compile-only selection unchanged when no AST is requested.
    if (ast) for ([_][]const u8{
        "evm.methodIdentifiers",
        "evm.bytecode.linkReferences",
        "evm.deployedBytecode.linkReferences",
        "evm.deployedBytecode.immutableReferences",
        "userdoc",
        "devdoc",
    }) |artifact| try artifacts.append(.{ .string = artifact });
    var contract_selection: std.json.Value = .{ .object = .empty };
    try contract_selection.object.put(arena, "*", .{ .array = artifacts });
    if (ast) {
        var source_artifacts = std.json.Array.init(arena);
        try source_artifacts.append(.{ .string = "ast" });
        try contract_selection.object.put(arena, "", .{ .array = source_artifacts });
    }
    var output_selection: std.json.Value = .{ .object = .empty };
    try output_selection.object.put(arena, "*", contract_selection);

    var settings: std.json.Value = .{ .object = .empty };
    try settings.object.put(arena, "optimizer", optimizer);
    try settings.object.put(arena, "outputSelection", output_selection);
    try settings.object.put(arena, "viaIR", .{ .bool = true });
    if (remapping_text) |contents| {
        var remappings = std.json.Array.init(arena);
        var lines = RemappingLines.init(contents);
        while (lines.next()) |text| try remappings.append(.{ .string = text });
        try settings.object.put(arena, "remappings", .{ .array = remappings });
    }
    try root.object.put(arena, "settings", settings);
    return JSON.jsonCompactPrintAlloc(allocator, &root);
}

/// Browser compilation requests source ASTs for navigation. Existing compiler
/// settings and artifact selections remain owned by the input request.
pub fn withAstAlloc(allocator: std.mem.Allocator, input: []const u8) ![]u8 {
    var arena = std.heap.ArenaAllocator.init(allocator);
    defer arena.deinit();
    const scratch = arena.allocator();
    var root = try std.json.parseFromSliceLeaky(std.json.Value, scratch, input, .{});
    const settings = objectChild(scratch, &root, "settings") catch |err| switch (err) {
        error.InvalidRequest => return allocator.dupe(u8, input),
        else => return err,
    };
    const selection = objectChild(scratch, settings, "outputSelection") catch |err| switch (err) {
        error.InvalidRequest => return allocator.dupe(u8, input),
        else => return err,
    };
    const wildcard = objectChild(scratch, selection, "*") catch |err| switch (err) {
        error.InvalidRequest => return allocator.dupe(u8, input),
        else => return err,
    };
    const entry = try wildcard.object.getOrPut(scratch, "");
    if (!entry.found_existing) entry.value_ptr.* = .{ .array = std.json.Array.init(scratch) };
    if (entry.value_ptr.* != .array) return allocator.dupe(u8, input);
    for (entry.value_ptr.array.items) |item| {
        if (item == .string and (std.mem.eql(u8, item.string, "ast") or std.mem.eql(u8, item.string, "*")))
            return allocator.dupe(u8, input);
    }
    try entry.value_ptr.array.append(.{ .string = "ast" });
    return JSON.jsonCompactPrintAlloc(allocator, &root);
}

fn objectChild(allocator: std.mem.Allocator, parent: *std.json.Value, key: []const u8) !*std.json.Value {
    if (parent.* != .object) return error.InvalidRequest;
    const entry = try parent.object.getOrPut(allocator, key);
    if (!entry.found_existing) entry.value_ptr.* = .{ .object = .empty };
    if (entry.value_ptr.* != .object) return error.InvalidRequest;
    return entry.value_ptr;
}

test "compile input enforces modern via-IR optimizer defaults" {
    const input = try buildAlloc(std.testing.allocator, &.{.{
        .name = "C.sol",
        .content = "contract C {}",
    }}, .{});
    defer std.testing.allocator.free(input);

    try std.testing.expect(std.mem.find(u8, input, "\"viaIR\":true") != null);
    try std.testing.expect(std.mem.find(u8, input, "\"enabled\":true") != null);
    try std.testing.expect(std.mem.find(u8, input, "evm.bytecode.object") != null);
    try std.testing.expect(std.mem.find(u8, input, "viaIR\":false") == null);
    try std.testing.expect(std.mem.find(u8, input, "abstractInterpretation") == null);
}

test "source remappings preserve context order and invalid entries for compiler validation" {
    const bytes = try buildAlloc(std.testing.allocator, &.{}, .{ .remappings = " pkg/=lib/pkg/\r\n\n src/:pkg/=vendor/pkg/\ninvalid\n" });
    defer std.testing.allocator.free(bytes);
    var parsed = try std.json.parseFromSlice(std.json.Value, std.testing.allocator, bytes, .{});
    defer parsed.deinit();
    const remappings = parsed.value.object.get("settings").?.object.get("remappings").?.array.items;
    try std.testing.expectEqual(@as(usize, 3), remappings.len);
    try std.testing.expectEqualStrings("pkg/=lib/pkg/", remappings[0].string);
    try std.testing.expectEqualStrings("src/:pkg/=vendor/pkg/", remappings[1].string);
    try std.testing.expectEqualStrings("invalid", remappings[2].string);
}

test "remappings reads allow only absent or bounded regular files" {
    const allocator = std.testing.allocator;
    const io = std.testing.io;
    var temporary = std.testing.tmpDir(.{});
    defer temporary.cleanup();
    const root = try temporary.dir.realPathFileAlloc(io, ".", allocator);
    defer allocator.free(root);
    try std.testing.expectEqual(@as(?[]u8, null), try readRemappingsAlloc(allocator, io, root));
    try temporary.dir.writeFile(io, .{ .sub_path = "remappings.txt", .data = "pkg/=lib/pkg/\n" });
    try std.testing.checkAllAllocationFailures(allocator, struct {
        fn run(alloc: std.mem.Allocator, project: []const u8) !void {
            const contents = (try readRemappingsAlloc(alloc, std.testing.io, project)).?;
            defer alloc.free(contents);
            try std.testing.expectEqualStrings("pkg/=lib/pkg/\n", contents);
        }
    }.run, .{root});
    const oversized = try allocator.alloc(u8, 1024 * 1024 + 1);
    defer allocator.free(oversized);
    @memset(oversized, 'x');
    try temporary.dir.writeFile(io, .{ .sub_path = "remappings.txt", .data = oversized });
    try std.testing.expectError(error.StreamTooLong, readRemappingsAlloc(allocator, io, root));
    try temporary.dir.deleteFile(io, "remappings.txt");
    try temporary.dir.createDir(io, "remappings.txt", .default_dir);
    try std.testing.expectError(error.RemappingsNotRegularFile, readRemappingsAlloc(allocator, io, root));
    const missing_root = try std.Io.Dir.path.join(allocator, &.{ root, "absent-project" });
    defer allocator.free(missing_root);
    try std.testing.expectError(error.FileNotFound, readRemappingsAlloc(allocator, io, missing_root));
}

test "remappings reads reject final and ancestor symlinks without disclosing contents" {
    if (@import("builtin").target.os.tag == .windows) return error.SkipZigTest;
    const allocator = std.testing.allocator;
    const io = std.testing.io;
    var temporary = std.testing.tmpDir(.{});
    defer temporary.cleanup();
    try temporary.dir.createDir(io, "project", .default_dir);
    try temporary.dir.writeFile(io, .{ .sub_path = "secret.env", .data = "SECRET=outside-project\n" });
    const root = try temporary.dir.realPathFileAlloc(io, "project", allocator);
    defer allocator.free(root);
    for ([_][]const u8{ "../secret.env", "../absent.env" }) |target| {
        try temporary.dir.symLink(io, target, "project/remappings.txt", .{});
        try std.testing.expectError(error.RemappingsNotRegularFile, readRemappingsAlloc(allocator, io, root));
        try temporary.dir.deleteFile(io, "project/remappings.txt");
    }
    try temporary.dir.writeFile(io, .{ .sub_path = "project/remappings.txt", .data = "pkg/=lib/\n" });
    try temporary.dir.symLink(io, "project", "linked", .{ .is_directory = true });
    const parent = try temporary.dir.realPathFileAlloc(io, ".", allocator);
    defer allocator.free(parent);
    const linked = try std.Io.Dir.path.join(allocator, &.{ parent, "linked" });
    defer allocator.free(linked);
    if (readRemappingsAlloc(allocator, io, linked)) |contents| {
        if (contents) |bytes| allocator.free(bytes);
        return error.TestUnexpectedResult;
    } else |err| switch (err) {
        error.NotDir, error.SymLinkLoop, error.AccessDenied => {},
        else => return err,
    }
}

test "project source names are stable for absolute paths and retain allocation ownership" {
    var temporary = std.testing.tmpDir(.{});
    defer temporary.cleanup();
    try temporary.dir.createDirPath(std.testing.io, "project/test");
    try temporary.dir.writeFile(std.testing.io, .{ .sub_path = "project/test/T.sol", .data = "contract T {}" });
    try temporary.dir.writeFile(std.testing.io, .{ .sub_path = "outside.sol", .data = "contract Outside {}" });
    const root = try temporary.dir.realPathFileAlloc(std.testing.io, "project", std.testing.allocator);
    defer std.testing.allocator.free(root);
    const path = try temporary.dir.realPathFileAlloc(std.testing.io, "project/test/T.sol", std.testing.allocator);
    defer std.testing.allocator.free(path);
    const outside = try temporary.dir.realPathFileAlloc(std.testing.io, "outside.sol", std.testing.allocator);
    defer std.testing.allocator.free(outside);
    // Keep OOM replay counts independent of SafeAllocator bucket occupancy.
    var deterministic = std.testing.FailingAllocator.init(std.testing.allocator, .{ .resize_fail_index = 0 });
    try std.testing.checkAllAllocationFailures(deterministic.allocator(), struct {
        fn run(allocator: std.mem.Allocator, project: []const u8, inside: []const u8, external: []const u8) !void {
            var loaded = try loadSourcesAt(allocator, std.testing.io, &.{ inside, external }, project);
            defer loaded.deinit();
            try std.testing.expectEqualStrings("test/T.sol", loaded.sources[0].name);
            try std.testing.expectEqualStrings(external, loaded.sources[1].name);
            try std.testing.expectEqualStrings("contract T {}", loaded.sources[0].content);
            try std.testing.expect(loaded.owned_names);
        }
    }.run, .{ root, path, outside });
}

test "browser AST selection preserves requested artifacts settings and invalid requests" {
    const input =
        \\{"language":"Solidity","sources":{},"settings":{"optimizer":{"enabled":true,"runs":42},"outputSelection":{"*":{"*":["abi"],"":["other"]},"C.sol":{"C":["evm.bytecode"]}}}}
    ;
    const output = try withAstAlloc(std.testing.allocator, input);
    defer std.testing.allocator.free(output);
    var parsed = try std.json.parseFromSlice(std.json.Value, std.testing.allocator, output, .{});
    defer parsed.deinit();
    const settings = parsed.value.object.get("settings").?.object;
    const selection = settings.get("outputSelection").?.object;
    try std.testing.expectEqual(@as(i64, 42), settings.get("optimizer").?.object.get("runs").?.integer);
    try std.testing.expect(selection.contains("C.sol"));
    const wildcard = selection.get("*").?.object;
    try std.testing.expectEqualStrings("abi", wildcard.get("*").?.array.items[0].string);
    try std.testing.expectEqualStrings("other", wildcard.get("").?.array.items[0].string);
    try std.testing.expectEqualStrings("ast", wildcard.get("").?.array.items[1].string);
    for ([_][]const u8{ "null", "{\"settings\":false}", "{\"settings\":{\"outputSelection\":{\"*\":{\"\":[\"ast\"]}}}}" }) |original| {
        const same = try withAstAlloc(std.testing.allocator, original);
        defer std.testing.allocator.free(same);
        try std.testing.expectEqualStrings(original, same);
    }
    var deterministic = std.testing.FailingAllocator.init(std.testing.allocator, .{ .resize_fail_index = 0 });
    try std.testing.checkAllAllocationFailures(deterministic.allocator(), struct {
        fn run(allocator: std.mem.Allocator) !void {
            const bytes = try withAstAlloc(allocator, "{\"language\":\"Solidity\",\"sources\":{}}");
            defer allocator.free(bytes);
        }
    }.run, .{});
}
