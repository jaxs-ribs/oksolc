// SPDX-License-Identifier: GPL-3.0
// Copyright (C) 2026 OKcontract Pte. Ltd.

//! Single-sample probes. Each baseline/candidate pair consumes identical inputs;
//! preparation and full item comparison are outside the measured intervals.
const std = @import("std");
const solidity = @import("solidity");
const Assembly = solidity.libevmasm.assembly.Assembly;
const Item = solidity.libevmasm.assembly_item.AssemblyItem;
const CacheModule = solidity.libevmasm.cse_chunk_cache;
const CFG = solidity.libyul.@"backends/evm/control_flow_graph";
const StackHelpers = solidity.libyul.@"backends/evm/stack_helpers";

// Metrics and lookup clocks are compiled out of ordinary compiler binaries.
pub const optimizer_cache_metrics = true;

const Case = struct {
    name: []const u8,
    chunks: usize = 64,
    stores: usize = 8,
    rounds: usize = 3,
    repeated: bool = false,
    max_entries: usize = 2048,
    max_payload_bytes: usize = 8 * 1024 * 1024,
};

const Measurement = struct {
    wall_nanoseconds: u64,
    changes: u32,
    output_items: usize,
    lookups: usize,
    hits: usize,
    copied_payload_bytes: usize,
    retained_payload_bytes: usize,
    retained_allocation_bytes: usize,
    entries: usize,
    lookup_nanoseconds: u64,
};

const Result = struct { name: []const u8, baseline: Measurement, cached: Measurement };

fn make(allocator: std.mem.Allocator, case: Case) !Assembly {
    var assembly = try Assembly.init(allocator, .current(), false, "cache-probe");
    errdefer assembly.deinit();
    for (0..case.chunks) |chunk| {
        for (0..case.stores) |index| {
            const value = if (case.repeated) index + 1 else chunk * case.stores + index + 1;
            _ = try assembly.append(Item.initPush(value, .{}));
            _ = try assembly.append(Item.initPush(index * 32, .{}));
            _ = try assembly.append(Item.initInstruction(.MSTORE, .{}));
        }
        _ = try assembly.append(Item.initInstruction(.STOP, .{}));
    }
    return assembly;
}

fn measure(io: std.Io, assembly: *Assembly, case: Case, comptime cached: bool) !Measurement {
    var owner = std.testing.FailingAllocator.init(std.heap.c_allocator, .{});
    var cache = CacheModule.Cache.init(owner.allocator());
    defer cache.deinit();
    cache.max_entries = case.max_entries;
    cache.max_payload_bytes = case.max_payload_bytes;
    cache.metrics_io = io;
    var changes: u32 = 0;
    const start = std.Io.Clock.awake.now(io);
    for (0..case.rounds) |_| try assembly.optimiseCSE(&changes, std.heap.c_allocator, if (cached) &cache else null);
    const elapsed = start.durationTo(std.Io.Clock.awake.now(io)).nanoseconds;
    return .{
        .wall_nanoseconds = @intCast(elapsed),
        .changes = changes,
        .output_items = assembly.itemsConst().len,
        .lookups = cache.lookups,
        .hits = cache.hits,
        .copied_payload_bytes = cache.copied_bytes,
        .retained_payload_bytes = cache.payload_bytes,
        .retained_allocation_bytes = owner.allocated_bytes - owner.freed_bytes,
        .entries = cache.entries.items.len,
        .lookup_nanoseconds = cache.lookup_nanoseconds,
    };
}

fn topSwap(io: std.Io, length: usize) !struct { slots: usize, wall_nanoseconds: u64, allocations: usize } {
    const Callbacks = struct {
        pub fn swap(_: @This(), _: usize) !void {}
        pub fn pushOrDup(_: @This(), _: CFG.StackSlot) !void {
            return error.UnexpectedPush;
        }
        pub fn pop(_: @This()) !void {
            return error.UnexpectedPop;
        }
    };
    var target: [1024]CFG.StackSlot = undefined;
    for (target[0..length], 0..) |*slot, index| slot.* = .{ .literal = .{ .value = index } };
    var current: CFG.Stack = .empty;
    defer current.deinit(std.heap.c_allocator);
    try current.appendSlice(std.heap.c_allocator, target[0..length]);
    std.mem.swap(CFG.StackSlot, &target[length - 1], &target[length - 2]);
    var rejecting = std.testing.FailingAllocator.init(std.heap.c_allocator, .{ .fail_index = 0 });
    const start = std.Io.Clock.awake.now(io);
    try StackHelpers.createStackLayout(rejecting.allocator(), &current, target[0..length], Callbacks{}, 16);
    const elapsed = start.durationTo(std.Io.Clock.awake.now(io)).nanoseconds;
    if (rejecting.has_induced_failure) return error.UnexpectedAllocation;
    for (current.items, target[0..length]) |actual, expected| if (!actual.eql(expected)) return error.IncorrectLayout;
    return .{ .slots = length, .wall_nanoseconds = @intCast(elapsed), .allocations = rejecting.allocations };
}

pub fn main(init: std.process.Init) !void {
    const cases = [_]Case{
        .{ .name = "unique-chunks" },
        .{ .name = "small-functions", .stores = 2 },
        .{ .name = "single-round", .rounds = 1 },
        .{ .name = "repeated-chunks", .repeated = true },
        .{ .name = "entry-limit", .max_entries = 8 },
        .{ .name = "payload-limit", .max_payload_bytes = 4096 },
    };
    var results: [cases.len]Result = undefined;
    for (cases, &results) |case, *result| {
        var plain = try make(std.heap.c_allocator, case);
        defer plain.deinit();
        var cached = try make(std.heap.c_allocator, case);
        defer cached.deinit();
        const baseline = try measure(init.io, &plain, case, false);
        const measured = try measure(init.io, &cached, case, true);
        if (baseline.changes != measured.changes or !CacheModule.equalItems(plain.itemsConst(), cached.itemsConst()))
            return error.CachedOutputMismatch;
        result.* = .{ .name = case.name, .baseline = baseline, .cached = measured };
    }
    const report = try std.json.Stringify.valueAlloc(init.gpa, .{
        .zig_version = @import("builtin").zig_version_string,
        .runs_per_case = 1,
        .warmups = 0,
        .timings_include_lookup_instrumentation = true,
        .cse = results,
        .top_swaps = .{ try topSwap(init.io, 16), try topSwap(init.io, 256), try topSwap(init.io, 1024) },
    }, .{ .whitespace = .indent_2 });
    defer init.gpa.free(report);
    try std.Io.File.stdout().writeStreamingAll(init.io, report);
    try std.Io.File.stdout().writeStreamingAll(init.io, "\n");
}
