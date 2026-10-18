// SPDX-License-Identifier: GPL-3.0
// Copyright (C) 2014-2026 The Solidity Authors.
// Copyright (C) 2026 OKcontract Pte. Ltd.

//! Capability-oriented translation of `CommonIO.cpp`.
//!
//! Unlike the C++ entry points, whole-input operations take an explicit size
//! limit and filesystem operations take both an I/O backend and directory
//! capability.  Returned byte slices are owned by the caller.

const std = @import("std");
const builtin = @import("builtin");

pub const ReadFileError = std.Io.Dir.StatFileError ||
    std.Io.Dir.ReadFileAllocError ||
    error{NotAFile};

pub fn formatBytesAlloc(
    allocator: std.mem.Allocator,
    bytes: []const u8,
) std.mem.Allocator.Error![]u8 {
    var output: std.ArrayList(u8) = .empty;
    errdefer output.deinit(allocator);
    try output.append(allocator, '[');
    for (bytes, 0..) |byte, index| {
        if (index != 0) try output.append(allocator, ',');
        const formatted = try allocator.print("{x}", .{byte});
        defer allocator.free(formatted);
        try output.appendSlice(allocator, formatted);
    }
    try output.append(allocator, ']');
    return output.toOwnedSlice(allocator);
}

/// Reads a regular file relative to `root`, following symlinks as upstream
/// does.  Directories and other non-regular filesystem nodes are rejected
/// before opening them for content reads.
pub fn readFileAsStringAlloc(
    allocator: std.mem.Allocator,
    io: std.Io,
    root: std.Io.Dir,
    path: []const u8,
    max_bytes: usize,
) ReadFileError![]u8 {
    const stat = try root.statFile(io, path, .{ .follow_symlinks = true });
    if (stat.kind != .file) return error.NotAFile;
    return root.readFileAlloc(io, path, allocator, .limited(max_bytes)) catch |err| switch (err) {
        // Preserve the C++ NotAFile distinction if the node changes between
        // the stat and open operations.
        error.IsDir => error.NotAFile,
        else => |other| other,
    };
}

/// Reads a stream through EOF, bounded by `max_bytes`.
pub fn readUntilEndAlloc(
    allocator: std.mem.Allocator,
    reader: *std.Io.Reader,
    max_bytes: usize,
) std.Io.Reader.LimitedAllocError![]u8 {
    return reader.allocRemaining(allocator, .limited(max_bytes));
}

/// Tries to read exactly `length` bytes and returns any shorter prefix read at
/// EOF.  This matches `std::istream::read` plus `gcount()` in the C++ source.
pub fn readBytesAlloc(
    allocator: std.mem.Allocator,
    reader: *std.Io.Reader,
    length: usize,
) (std.mem.Allocator.Error || std.Io.Reader.ShortError)![]u8 {
    var output = try allocator.alloc(u8, length);
    errdefer allocator.free(output);
    const actual_length = try reader.readSliceShort(output);
    if (actual_length == output.len) return output;
    output = try allocator.realloc(output, actual_length);
    return output;
}

pub const StandardInputError = std.Io.File.Reader.Error ||
    std.posix.TermiosGetError ||
    std.posix.TermiosSetError;

/// Reads one byte from stdin without waiting for an end-of-line on POSIX
/// terminals. Non-terminal streams remain readable and platforms without the
/// POSIX terminal interface use the ordinary one-byte input path.
pub fn readStandardInputChar(io: std.Io) StandardInputError!?u8 {
    const supports_termios = switch (builtin.target.os.tag) {
        .linux,
        .driverkit,
        .ios,
        .maccatalyst,
        .macos,
        .tvos,
        .visionos,
        .watchos,
        .freebsd,
        .netbsd,
        .openbsd,
        .dragonfly,
        .haiku,
        .illumos,
        => true,
        else => false,
    };
    if (comptime supports_termios) {
        const original = std.posix.tcgetattr(std.posix.STDIN_FILENO) catch |err| switch (err) {
            // Redirected stdin is not a terminal and needs no mode change.
            error.NotATerminal => return readInputByte(io),
            else => |other| return other,
        };
        var unbuffered = original;
        unbuffered.lflag.ICANON = false;
        unbuffered.lflag.ECHO = false;
        unbuffered.cc[@backingInt(std.posix.V.MIN)] = 1;
        unbuffered.cc[@backingInt(std.posix.V.TIME)] = 0;
        try std.posix.tcsetattr(std.posix.STDIN_FILENO, .NOW, unbuffered);
        const read_result = readInputByte(io);
        const restore_result = std.posix.tcsetattr(std.posix.STDIN_FILENO, .DRAIN, original);
        const byte = try read_result;
        try restore_result;
        return byte;
    }
    return readInputByte(io);
}

fn readInputByte(io: std.Io) std.Io.File.Reader.Error!?u8 {
    var file_reader = std.Io.File.stdin().reader(io, &.{});
    var byte: [1]u8 = undefined;
    const count = file_reader.interface.readSliceShort(&byte) catch
        return file_reader.err.?;
    return if (count == 0) null else byte[0];
}

/// Allocating equivalent of the upstream stream-based `toString` helper.
pub fn toStringAlloc(allocator: std.mem.Allocator, value: anytype) std.mem.Allocator.Error![]u8 {
    return allocator.print("{any}", .{value});
}

/// Resolves dot-relative source-unit names without interpreting the existing
/// reference as a host filesystem path. Earlier components retain their exact
/// spelling, including URL-like prefixes and repeated separators.
pub fn absolutePathAlloc(
    allocator: std.mem.Allocator,
    path: []const u8,
    reference: []const u8,
) std.mem.Allocator.Error![]u8 {
    if (!std.mem.eql(u8, path, ".") and !std.mem.eql(u8, path, "..") and
        !std.mem.startsWith(u8, path, "./") and !std.mem.startsWith(u8, path, "../"))
        return allocator.dupe(u8, path);

    var result: std.ArrayList(u8) = .empty;
    errdefer result.deinit(allocator);
    try result.appendSlice(allocator, reference);
    // Root directories, including //namespace/, have no filename to remove.
    const filename_end = std.mem.trimEnd(u8, reference, "/").len;
    const root_only = filename_end == 0 or
        (filename_end != reference.len and filename_end == sourceRootNameEnd(reference));
    if (!root_only)
        result.items.len = sourceParentEnd(result.items);
    var components = std.mem.tokenizeScalar(u8, path, '/');
    while (components.next()) |component| {
        if (std.mem.eql(u8, component, ".")) continue;
        if (std.mem.eql(u8, component, "..")) {
            result.items.len = sourceParentEnd(result.items);
        } else {
            if (result.items.len != 0 and result.items[result.items.len - 1] != '/')
                try result.append(allocator, '/');
            try result.appendSlice(allocator, component);
        }
    }
    return result.toOwnedSlice(allocator);
}

/// The POSIX spelling used by Solidity's virtual namespace is independent of
/// the host OS. Remove one component without normalizing earlier components.
fn sourceParentEnd(name: []const u8) usize {
    if (name.len == 0) return 0;
    if (name[name.len - 1] == '/') return std.mem.trimEnd(u8, name, "/").len;
    const separator = std.mem.findScalarLast(u8, name, '/') orelse return 0;
    const end = std.mem.trimEnd(u8, name[0..separator], "/").len;
    if (end == 0) {
        // A bare //name is a namespace root name, rather than / plus a file.
        if (separator == 1) return 0;
        return 1;
    }
    const root_name_end = sourceRootNameEnd(name);
    if (root_name_end != 0 and end == root_name_end) return root_name_end + 1;
    return end;
}

fn sourceRootNameEnd(name: []const u8) usize {
    if (name.len < 3 or !std.mem.startsWith(u8, name, "//") or name[2] == '/') return 0;
    return std.mem.findScalarPos(u8, name, 2, '/') orelse name.len;
}

/// Converts native path separators to the generic `/` spelling used by the
/// C++ `boost::filesystem::path::generic_string()` result.
pub fn sanitizePathAlloc(
    allocator: std.mem.Allocator,
    path: []const u8,
) std.mem.Allocator.Error![]u8 {
    const result = try allocator.dupe(u8, path);
    if (builtin.target.os.tag == .windows) std.mem.replaceScalar(u8, result, '\\', '/');
    return result;
}

test "bounded stream helpers preserve bytes and partial reads" {
    var reader: std.Io.Reader = .fixed("ABC\r\ndef");
    const first = try readBytesAlloc(std.testing.allocator, &reader, 4);
    defer std.testing.allocator.free(first);
    try std.testing.expectEqualStrings("ABC\r", first);

    const rest = try readUntilEndAlloc(std.testing.allocator, &reader, 16);
    defer std.testing.allocator.free(rest);
    try std.testing.expectEqualStrings("\ndef", rest);

    const empty = try readBytesAlloc(std.testing.allocator, &reader, 20);
    defer std.testing.allocator.free(empty);
    try std.testing.expectEqual(@as(usize, 0), empty.len);
}

test "whole-input limits are enforced" {
    var reader: std.Io.Reader = .fixed("abcd");
    try std.testing.expectError(
        error.StreamTooLong,
        readUntilEndAlloc(std.testing.allocator, &reader, 3),
    );
}

test "file reads reject directories and preserve content" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    try tmp.dir.writeFile(std.testing.io, .{
        .sub_path = "test.txt",
        .data = "ABC\ndef\n",
    });

    const content = try readFileAsStringAlloc(
        std.testing.allocator,
        std.testing.io,
        tmp.dir,
        "test.txt",
        1024,
    );
    defer std.testing.allocator.free(content);
    try std.testing.expectEqualStrings("ABC\ndef\n", content);
    try std.testing.expectError(
        error.NotAFile,
        readFileAsStringAlloc(
            std.testing.allocator,
            std.testing.io,
            tmp.dir,
            ".",
            1024,
        ),
    );
}

test "absolutePath resolves only explicit dot-relative paths" {
    const allocator = std.testing.allocator;
    const unchanged = try absolutePathAlloc(allocator, "contracts/A.sol", "/src/Main.sol");
    defer allocator.free(unchanged);
    try std.testing.expectEqualStrings("contracts/A.sol", unchanged);

    const child = try absolutePathAlloc(allocator, "./A.sol", "/src/nested/Main.sol");
    defer allocator.free(child);
    try std.testing.expectEqualStrings("/src/nested/A.sol", child);

    const parent = try absolutePathAlloc(allocator, "../A.sol", "/src/nested/Main.sol");
    defer allocator.free(parent);
    try std.testing.expectEqualStrings("/src/A.sol", parent);
}

test "absolutePath preserves virtual namespaces and resolves only appended components" {
    const cases = [_]struct { reference: []const u8, path: []const u8, expected: []const u8 }{
        .{ .reference = "proto://dir/Main.sol", .path = "./Policy.sol", .expected = "proto://dir/Policy.sol" },
        .{ .reference = "proto://dir/Main.sol", .path = "../Policy.sol", .expected = "proto:/Policy.sol" },
        .{ .reference = "proto://dir/Main.sol", .path = "../../Policy.sol", .expected = "Policy.sol" },
        .{ .reference = "proto://dir/Main.sol", .path = "./x//y/./../Policy.sol", .expected = "proto://dir/x/Policy.sol" },
        .{ .reference = "a//b///Main.sol", .path = "./Policy.sol", .expected = "a//b/Policy.sol" },
        .{ .reference = "a//b///Main.sol", .path = "../Policy.sol", .expected = "a/Policy.sol" },
        .{ .reference = "a/../Main.sol", .path = "./Policy.sol", .expected = "a/../Policy.sol" },
        .{ .reference = "a/./Main.sol", .path = "../Policy.sol", .expected = "a/Policy.sol" },
        .{ .reference = "../../Main.sol", .path = "../../Policy.sol", .expected = "Policy.sol" },
        .{ .reference = "Main.sol", .path = "../../../Policy.sol", .expected = "Policy.sol" },
        .{ .reference = "/Main.sol", .path = "../../Policy.sol", .expected = "Policy.sol" },
        .{ .reference = "///Main.sol", .path = "./Policy.sol", .expected = "/Policy.sol" },
        .{ .reference = "///a//b///Main.sol", .path = "../Policy.sol", .expected = "///a/Policy.sol" },
        .{ .reference = "//server/share/Main.sol", .path = "../..", .expected = "//server" },
        .{ .reference = "//server/Main.sol", .path = "../../Policy.sol", .expected = "Policy.sol" },
        .{ .reference = "//server", .path = "./Policy.sol", .expected = "Policy.sol" },
        .{ .reference = "//server/", .path = "..", .expected = "//server" },
        .{ .reference = "C:/dir/Main.sol", .path = "../Policy.sol", .expected = "C:/Policy.sol" },
        .{ .reference = "C:\\dir\\Main.sol", .path = "./Policy.sol", .expected = "Policy.sol" },
        .{ .reference = "a/b/", .path = "./Policy.sol", .expected = "a/b/Policy.sol" },
        .{ .reference = "proto://dir/Main.sol", .path = "./a///", .expected = "proto://dir/a" },
        .{ .reference = "/", .path = "./Policy.sol", .expected = "/Policy.sol" },
        .{ .reference = "///", .path = "./Policy.sol", .expected = "///Policy.sol" },
        .{ .reference = "proto://dir/Main.sol", .path = "/./Policy.sol", .expected = "/./Policy.sol" },
        .{ .reference = "proto://dir/Main.sol", .path = ".\\Policy.sol", .expected = ".\\Policy.sol" },
    };
    for (cases) |case| {
        const actual = try absolutePathAlloc(std.testing.allocator, case.path, case.reference);
        defer std.testing.allocator.free(actual);
        try std.testing.expectEqualStrings(case.expected, actual);
    }
    try std.testing.checkAllAllocationFailures(std.testing.allocator, struct {
        fn run(allocator: std.mem.Allocator) !void {
            const actual = try absolutePathAlloc(allocator, "./x/../Policy.sol", "proto://dir/Main.sol");
            defer allocator.free(actual);
            try std.testing.expectEqualStrings("proto://dir/Policy.sol", actual);
        }
    }.run, .{});
}

test "toString owns its result" {
    const result = try toStringAlloc(std.testing.allocator, @as(u32, 42));
    defer std.testing.allocator.free(result);
    try std.testing.expectEqualStrings("42", result);

    const bytes = try formatBytesAlloc(std.testing.allocator, &.{ 0, 10, 255 });
    defer std.testing.allocator.free(bytes);
    try std.testing.expectEqualStrings("[0,a,ff]", bytes);
}
