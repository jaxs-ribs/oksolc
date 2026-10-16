// SPDX-License-Identifier: GPL-3.0
// Copyright (C) 2026 OKcontract Pte. Ltd.

//! Exact deficits for one shuffle, indexed by stack-slot equality classes.
//! Targets stay immutable; only the shuffler mutates the source. Initial slot
//! pointers are borrowed solely while interning, before any callback runs.

const std = @import("std");
const builtin = @import("builtin");
const CFG = @import("control_flow_graph.zig");

const IdentityContext = struct {
    hashes: if (builtin.is_test) ?*usize else void = if (builtin.is_test) null else {},
    comparisons: if (builtin.is_test) ?*usize else void = if (builtin.is_test) null else {},

    pub fn hash(self: @This(), slot: *const CFG.StackSlot) u64 {
        if (builtin.is_test) if (self.hashes) |count| {
            count.* += 1;
        };
        const tag: u64 = @intCast(@backingInt(slot.*));
        return switch (slot.*) {
            .literal => |value| std.hash.Wyhash.hash(tag, std.mem.asBytes(&value.value)),
            .temporary => |value| blk: {
                const words = [2]usize{ @intFromPtr(value.call), value.index };
                break :blk std.hash.Wyhash.hash(tag, std.mem.asBytes(&words));
            },
            .function_call_return_label => |value| pointerHash(tag, value.call),
            .function_return_label => |value| pointerHash(tag, value.function),
            .variable => |value| pointerHash(tag, value.variable),
            .junk => std.hash.Wyhash.hash(tag, &.{}),
        };
    }

    fn pointerHash(tag: u64, pointer: anytype) u64 {
        const address = @intFromPtr(pointer);
        return std.hash.Wyhash.hash(tag, std.mem.asBytes(&address));
    }

    pub fn eql(self: @This(), left: *const CFG.StackSlot, right: *const CFG.StackSlot) bool {
        if (builtin.is_test) if (self.comparisons) |count| {
            count.* += 1;
        };
        return left.eql(right.*);
    }
};

const Interner = struct {
    // Keep short layouts in a compact linear representation. Larger layouts
    // use borrowed keys with exactly StackSlot.eql's identity (no debug data).
    representatives: std.ArrayList(*const CFG.StackSlot) = .empty,
    indices: std.HashMapUnmanaged(*const CFG.StackSlot, usize, IdentityContext, 80) = .empty,
    context: IdentityContext,
    count: usize = 0,
    hashed: bool,

    fn init(allocator: std.mem.Allocator, capacity: usize, context: IdentityContext) !Interner {
        var result: Interner = .{ .context = context, .hashed = capacity > 32 };
        if (result.hashed) {
            const size = std.math.cast(u32, capacity) orelse return error.OutOfMemory;
            try result.indices.ensureTotalCapacityContext(allocator, size, context);
        } else {
            result.representatives = try .initCapacity(allocator, capacity);
        }
        return result;
    }

    fn deinit(self: *Interner, allocator: std.mem.Allocator) void {
        self.indices.deinit(allocator);
        self.representatives.deinit(allocator);
    }

    fn intern(self: *Interner, slot: *const CFG.StackSlot) usize {
        if (self.hashed) {
            const entry = self.indices.getOrPutAssumeCapacityContext(slot, self.context);
            if (entry.found_existing) return entry.value_ptr.*;
            entry.value_ptr.* = self.count;
        } else {
            for (self.representatives.items, 0..) |representative, id|
                if (self.context.eql(representative, slot)) return id;
            self.representatives.appendAssumeCapacity(slot);
        }
        const id = self.count;
        self.count += 1;
        return id;
    }
};

pub const StackMultiplicity = struct {
    allocator: std.mem.Allocator,
    source_ids: std.ArrayList(usize),
    target_ids: []usize,
    deficits: []i32,

    pub fn init(allocator: std.mem.Allocator, source: []const CFG.StackSlot, target: []const CFG.StackSlot) !StackMultiplicity {
        return initWithContext(allocator, source, target, .{});
    }

    fn initWithContext(allocator: std.mem.Allocator, source: []const CFG.StackSlot, target: []const CFG.StackSlot, context: IdentityContext) !StackMultiplicity {
        const target_capacity = std.math.add(usize, target.len, 1) catch return error.OutOfMemory;
        const class_capacity = std.math.add(usize, source.len, target_capacity) catch return error.OutOfMemory;
        // Every push site runs with source size <= target size. A shuffle can
        // therefore temporarily exceed the target by one slot, but no more.
        var source_ids = try std.ArrayList(usize).initCapacity(allocator, @max(source.len, target_capacity));
        errdefer source_ids.deinit(allocator);
        const target_ids = try allocator.alloc(usize, target.len);
        errdefer allocator.free(target_ids);
        const deficits = try allocator.alloc(i32, class_capacity);
        errdefer allocator.free(deficits);
        @memset(deficits, 0);
        var interner = try Interner.init(allocator, class_capacity, context);
        defer interner.deinit(allocator);
        // Class zero always represents junk, including currently absent target
        // positions. It is a value identity, not an arbitrary-slot wildcard.
        const junk: CFG.StackSlot = .{ .junk = .{} };
        const junk_id = interner.intern(&junk);
        std.debug.assert(junk_id == 0);
        for (source) |*slot| {
            const id = interner.intern(slot);
            source_ids.appendAssumeCapacity(id);
            deficits[id] -= 1;
        }
        for (target, 0..) |*slot, offset| {
            const id = interner.intern(slot);
            target_ids[offset] = id;
            const effective = if (id == 0 and offset < source.len) source_ids.items[offset] else id;
            deficits[effective] += 1;
        }
        return .{ .allocator = allocator, .source_ids = source_ids, .target_ids = target_ids, .deficits = deficits };
    }

    pub fn deinit(self: *StackMultiplicity) void {
        self.allocator.free(self.deficits);
        self.allocator.free(self.target_ids);
        self.source_ids.deinit(self.allocator);
        self.* = undefined;
    }

    pub fn sourceMultiplicity(self: *const StackMultiplicity, offset: usize) i32 {
        return self.deficits[self.source_ids.items[offset]];
    }

    pub fn targetMultiplicity(self: *const StackMultiplicity, offset: usize) i32 {
        return self.deficits[self.target_ids[offset]];
    }

    fn arbitrary(self: *const StackMultiplicity, offset: usize) bool {
        return offset < self.target_ids.len and self.target_ids[offset] == 0;
    }

    pub fn swap(self: *StackMultiplicity, depth: usize) void {
        const top = self.source_ids.items.len - 1;
        const other = top - depth;
        const a = self.source_ids.items[other];
        const b = self.source_ids.items[top];
        // Source counts stay fixed; only effective target-junk values move.
        if (self.arbitrary(other)) {
            self.deficits[a] -= 1;
            self.deficits[b] += 1;
        }
        if (self.arbitrary(top)) {
            self.deficits[b] -= 1;
            self.deficits[a] += 1;
        }
        std.mem.swap(usize, &self.source_ids.items[other], &self.source_ids.items[top]);
    }

    pub fn pop(self: *StackMultiplicity) void {
        const offset = self.source_ids.items.len - 1;
        const id = self.source_ids.pop().?;
        self.deficits[id] += 1;
        if (self.arbitrary(offset)) {
            self.deficits[id] -= 1;
            self.deficits[0] += 1;
        }
    }

    pub fn push(self: *StackMultiplicity, target_offset: usize) void {
        const offset = self.source_ids.items.len;
        const id = self.target_ids[target_offset];
        self.source_ids.appendAssumeCapacity(id);
        self.deficits[id] -= 1;
        if (self.arbitrary(offset)) {
            self.deficits[0] -= 1;
            self.deficits[id] += 1;
        }
    }
};

test "stack multiplicity interning scales linearly for distinct large stacks" {
    var slots: [1024]CFG.StackSlot = undefined;
    for (&slots, 0..) |*slot, index| slot.* = .{ .literal = .{ .value = index } };
    for ([_]usize{ 16, 256, 1024 }) |length| {
        var hashes: usize = 0;
        var comparisons: usize = 0;
        var counts = try StackMultiplicity.initWithContext(std.testing.allocator, slots[0..length], slots[0..length], .{
            .hashes = &hashes,
            .comparisons = &comparisons,
        });
        defer counts.deinit();
        try std.testing.expect(hashes <= 3 * length + 4);
        try std.testing.expect(comparisons < 16 * length);
        for (0..length) |index| try std.testing.expectEqual(@as(i32, 0), counts.sourceMultiplicity(index));
    }
}

test "stack multiplicity hashed identity ignores debug data and distinguishes slot kinds" {
    const AST = @import("../../ast.zig");
    const Scope = @import("../../scope.zig");
    var call: AST.FunctionCall = undefined;
    var function: Scope.Function = undefined;
    var variable: Scope.Variable = undefined;
    const slots = [_]CFG.StackSlot{
        .{ .function_call_return_label = .{ .call = &call } },
        .{ .function_return_label = .{ .function = &function } },
        .{ .variable = .{ .variable = &variable } },
        .{ .variable = .{ .variable = &variable, .debug_data = .{ .ast_id = 9 } } },
        .{ .literal = .{ .value = std.math.maxInt(u256) } },
        .{ .literal = .{ .value = std.math.maxInt(u256), .debug_data = .{ .ast_id = 7 } } },
        .{ .temporary = .{ .call = &call, .index = 0 } },
        .{ .temporary = .{ .call = &call, .index = 1 } },
        .{ .junk = .{} },
    };
    var interner = try Interner.init(std.testing.allocator, 64, .{});
    defer interner.deinit(std.testing.allocator);
    for (&slots, 0..) |*left, index| {
        const id = interner.intern(left);
        for (slots[0..index], 0..) |right, previous|
            try std.testing.expectEqual(left.eql(right), id == interner.intern(&slots[previous]));
    }
}

test "stack multiplicity cache initialization unwinds every partial allocation" {
    const Check = struct {
        fn run(allocator: std.mem.Allocator) !void {
            var slots: [256]CFG.StackSlot = undefined;
            for (&slots, 0..) |*slot, index| slot.* = .{ .literal = .{ .value = index } };
            var counts = try StackMultiplicity.init(allocator, &slots, &slots);
            defer counts.deinit();
        }
    };
    var deterministic = std.testing.FailingAllocator.init(std.testing.allocator, .{ .resize_fail_index = 0 });
    try std.testing.checkAllAllocationFailures(deterministic.allocator(), Check.run, .{});
}
