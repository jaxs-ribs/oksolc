// SPDX-License-Identifier: GPL-3.0
// Copyright (C) 2026 OKcontract Pte. Ltd.

//! Bounded, per-assembly reuse of complete fresh-state CSE chunk outcomes.
//! Inputs and replacements are owned outside chunk scratch. Source names retain
//! AssemblyItem's immutable compilation-lifetime borrow. A hash never proves a
//! match: all input fields, revision and MSIZE context must compare exactly.
const std = @import("std");
const Item = @import("assembly_item.zig").AssemblyItem;
const EVMVersion = @import("../liblangutil/evm_version.zig").EVMVersion;
const builtin = @import("builtin");

pub const Outcome = enum { unchanged, replacement, stack_too_deep, item_not_available };
pub const Request = struct {
    input: []const Item,
    version: EVMVersion,
    msize: bool,
    hash: u64,
};
pub const Entry = struct {
    input: []Item,
    output: []Item,
    version: EVMVersion,
    msize: bool,
    outcome: Outcome,
    previous: ?u32,
};

pub const Cache = struct {
    allocator: std.mem.Allocator,
    heads: std.AutoHashMapUnmanaged(u64, u32) = .empty,
    entries: std.ArrayList(Entry) = .empty,
    min_items: usize = 16,
    max_entries: usize = 2048,
    max_payload_bytes: usize = 8 * 1024 * 1024,
    payload_bytes: usize = 0,
    hits: if (builtin.is_test) usize else u0 = 0,
    replacement_hits: if (builtin.is_test) usize else u0 = 0,
    hash_mask: if (builtin.is_test) u64 else u0 = if (builtin.is_test) std.math.maxInt(u64) else 0,

    pub fn init(allocator: std.mem.Allocator) Cache {
        return .{ .allocator = allocator };
    }

    pub fn deinit(self: *Cache) void {
        for (self.entries.items) |entry| {
            destroy(self.allocator, entry.input);
            destroy(self.allocator, entry.output);
        }
        self.entries.deinit(self.allocator);
        self.heads.deinit(self.allocator);
        self.* = undefined;
    }

    /// The input remains immutable until lookup/record complete. Compute its
    /// fingerprint once, before the original analysis runs on a cache miss.
    pub fn request(self: *const Cache, input: []const Item, version: EVMVersion, msize: bool) Request {
        return .{ .input = input, .version = version, .msize = msize, .hash = if (input.len >= self.min_items) self.key(input, version, msize) else 0 };
    }

    /// The returned view lasts until the next insertion or cache destruction.
    pub fn lookup(self: *Cache, query: Request) ?*const Entry {
        const input = query.input;
        if (input.len < self.min_items) return null;
        var index = self.heads.get(query.hash);
        while (index) |current| {
            const entry = &self.entries.items[current];
            if (entry.version.version == query.version.version and entry.msize == query.msize and equalItems(entry.input, input)) {
                if (builtin.is_test) {
                    self.hits += 1;
                    if (entry.outcome == .replacement) self.replacement_hits += 1;
                }
                return entry;
            }
            index = entry.previous;
        }
        return null;
    }

    pub fn record(self: *Cache, query: Request, output: []const Item, outcome: Outcome) !void {
        const input = query.input;
        if (input.len < self.min_items) return;
        std.debug.assert(if (outcome == .replacement) output.len < input.len else output.len == 0);
        const bytes = payloadSize(input) +| payloadSize(output);
        if (self.entries.items.len >= self.max_entries or bytes > self.max_payload_bytes -| self.payload_bytes) return;
        const owned_input = try clone(self.allocator, input);
        errdefer destroy(self.allocator, owned_input);
        const owned_output = try clone(self.allocator, output);
        errdefer destroy(self.allocator, owned_output);
        try self.entries.ensureUnusedCapacity(self.allocator, 1);
        const head = try self.heads.getOrPut(self.allocator, query.hash);
        const previous = if (head.found_existing) head.value_ptr.* else null;
        const index: u32 = @intCast(self.entries.items.len);
        self.entries.appendAssumeCapacity(.{
            .input = owned_input,
            .output = owned_output,
            .version = query.version,
            .msize = query.msize,
            .outcome = outcome,
            .previous = previous,
        });
        head.value_ptr.* = index;
        self.payload_bytes += bytes;
    }

    fn key(self: *const Cache, input: []const Item, version: EVMVersion, msize: bool) u64 {
        var hash: u64 = @as(u64, @intCast(@intFromEnum(version.version))) | (@as(u64, @intFromBool(msize)) << 32);
        for (input) |item| {
            // Fixed words avoid streaming tiny fragments through a hash buffer.
            // Omitted fields are still checked by equalItems on every hit.
            const words = [_]u64{
                @as(u64, @intCast(@intFromEnum(item.item_type))) | (@as(u64, if (item.instruction_value) |op| @intFromEnum(op) else 256) << 32),
                @truncate(item.data_value),
                @truncate(item.data_value >> 64),
                @truncate(item.data_value >> 128),
                @truncate(item.data_value >> 192),
                locations(item.debug_data.native_location.start, item.debug_data.native_location.end),
                locations(item.debug_data.origin_location.start, item.debug_data.origin_location.end),
                if (item.debug_data.ast_id) |id| @bitCast(id) else 0,
                @intCast(item.modifier_depth),
            };
            hash = std.hash.Wyhash.hash(hash, std.mem.asBytes(&words));
            if (item.verbatim) |verbatim| hash = std.hash.Wyhash.hash(hash, verbatim.data);
        }
        return if (builtin.is_test) hash & self.hash_mask else hash;
    }
};

fn locations(start: i32, end: i32) u64 {
    return @as(u64, @as(u32, @bitCast(start))) << 32 | @as(u32, @bitCast(end));
}

pub fn equalItems(left: []const Item, right: []const Item) bool {
    if (left.len != right.len) return false;
    for (left, right) |a, b| {
        if (a.item_type != b.item_type or a.instruction_value != b.instruction_value or
            a.data_value != b.data_value or a.jump_type != b.jump_type or
            a.pushed_value != b.pushed_value or a.immutable_occurrences != b.immutable_occurrences or
            a.modifier_depth != b.modifier_depth or a.debug_data.ast_id != b.debug_data.ast_id or
            !a.debug_data.native_location.eql(b.debug_data.native_location) or
            !a.debug_data.origin_location.eql(b.debug_data.origin_location)) return false;
        if (a.verbatim) |av| {
            const bv = b.verbatim orelse return false;
            if (av.arguments != bv.arguments or av.return_variables != bv.return_variables or
                !std.mem.eql(u8, av.data, bv.data)) return false;
        } else if (b.verbatim != null) return false;
    }
    return true;
}

fn payloadSize(items: []const Item) usize {
    var bytes = items.len *| @sizeOf(Item);
    for (items) |item| if (item.verbatim) |verbatim| {
        bytes +|= verbatim.data.len;
    };
    return bytes;
}

fn clone(allocator: std.mem.Allocator, input: []const Item) ![]Item {
    const result = try allocator.alloc(Item, input.len);
    var initialized: usize = 0;
    errdefer {
        for (result[0..initialized]) |*item| item.deinit(allocator);
        allocator.free(result);
    }
    for (input, result) |*item, *out| {
        out.* = try item.clone(allocator);
        initialized += 1;
    }
    return result;
}

fn destroy(allocator: std.mem.Allocator, items: []Item) void {
    for (items) |*item| item.deinit(allocator);
    allocator.free(items);
}

test "assembly CSE cache checks collisions context and every annotation" {
    const allocator = std.testing.allocator;
    var cache = Cache.init(allocator);
    defer cache.deinit();
    cache.min_items = 1;
    cache.hash_mask = 0;
    var input = [_]Item{ Item.initPush(7, .{ .ast_id = 3 }), Item.initInstruction(.POP, .{}) };
    const output = [_]Item{Item.initPush(8, .{ .ast_id = 4 })};
    const query = cache.request(&input, .current(), false);
    try cache.record(query, &output, .replacement);
    try std.testing.expectEqual(Outcome.replacement, cache.lookup(query).?.outcome);
    try std.testing.expect(equalItems(&output, cache.lookup(query).?.output));
    try std.testing.expect(cache.lookup(cache.request(&input, .init(.London), false)) == null);
    try std.testing.expect(cache.lookup(cache.request(&input, .current(), true)) == null);
    const original = input[0];
    for (0..14) |field| {
        input[0] = original;
        switch (field) {
            0 => input[0].item_type = .PushTag,
            1 => input[0].instruction_value = .ADD,
            2 => input[0].data_value = 9,
            3 => input[0].jump_type = .IntoFunction,
            4 => input[0].pushed_value = 0,
            5 => input[0].immutable_occurrences = 0,
            6 => input[0].modifier_depth = 1,
            7 => input[0].debug_data.ast_id = 4,
            8 => input[0].debug_data.native_location.start = 1,
            9 => input[0].debug_data.native_location.end = 1,
            10 => input[0].debug_data.native_location.source_name = "other.sol",
            11 => input[0].debug_data.origin_location.start = 1,
            12 => input[0].debug_data.origin_location.end = 1,
            13 => input[0].debug_data.origin_location.source_name = "other.sol",
            else => unreachable,
        }
        try std.testing.expect(cache.lookup(cache.request(&input, .current(), false)) == null);
    }
    input[0] = Item.initPush(9, .{});
    const collision = cache.request(&input, .current(), false);
    try cache.record(collision, &.{}, .stack_too_deep);
    try std.testing.expectEqual(Outcome.stack_too_deep, cache.lookup(collision).?.outcome);
    input[0] = original;
    try std.testing.expectEqual(Outcome.replacement, cache.lookup(cache.request(&input, .current(), false)).?.outcome);
    cache.max_payload_bytes = 0;
    const count = cache.entries.items.len;
    try cache.record(query, &.{}, .unchanged);
    try std.testing.expectEqual(count, cache.entries.items.len);
    try std.testing.expect(cache.payload_bytes <= 8 * 1024 * 1024);
}

test "assembly CSE cache owns verbatim data and unwinds allocation failures" {
    const Exercise = struct {
        fn run(allocator: std.mem.Allocator) !void {
            var cache = Cache.init(allocator);
            defer cache.deinit();
            cache.min_items = 1;
            cache.hash_mask = 0;
            var input = [_]Item{try Item.initVerbatim(allocator, &.{ 0x60, 0x01 }, 0, 1)};
            defer input[0].deinit(allocator);
            const query = cache.request(&input, .current(), true);
            try cache.record(query, &.{}, .item_not_available);
            try std.testing.expectEqual(Outcome.item_not_available, cache.lookup(query).?.outcome);
            input[0].verbatim.?.data[1] = 2;
            try std.testing.expect(cache.lookup(cache.request(&input, .current(), true)) == null);
            try std.testing.expectEqual(@as(u8, 1), cache.entries.items[0].input[0].verbatim.?.data[1]);
            input[0].verbatim.?.data[1] = 1;
            input[0].verbatim.?.arguments = 1;
            try std.testing.expect(cache.lookup(cache.request(&input, .current(), true)) == null);
            input[0].verbatim.?.arguments = 0;
            input[0].verbatim.?.return_variables = 2;
            try std.testing.expect(cache.lookup(cache.request(&input, .current(), true)) == null);
        }
    };
    try Exercise.run(std.testing.allocator);
    try std.testing.checkAllAllocationFailures(std.testing.allocator, Exercise.run, .{});
}
