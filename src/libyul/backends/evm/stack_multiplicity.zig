// SPDX-License-Identifier: GPL-3.0
// Copyright (C) 2026 OKcontract Pte. Ltd.

//! Exact deficits for one shuffle, indexed by stack-slot equality classes.
//! Targets stay immutable; only the shuffler mutates the source. Initial slot
//! pointers are borrowed solely while interning, before any callback runs.

const std = @import("std");
const CFG = @import("control_flow_graph.zig");

pub const StackMultiplicity = struct {
    allocator: std.mem.Allocator,
    source_ids: std.ArrayList(usize),
    target_ids: []usize,
    deficits: []i32,

    pub fn init(allocator: std.mem.Allocator, source: []const CFG.StackSlot, target: []const CFG.StackSlot) !StackMultiplicity {
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
        var representatives = try std.ArrayList(*const CFG.StackSlot).initCapacity(allocator, class_capacity);
        defer representatives.deinit(allocator);
        // Class zero always represents junk, including currently absent target
        // positions. It is a value identity, not an arbitrary-slot wildcard.
        const junk: CFG.StackSlot = .{ .junk = .{} };
        representatives.appendAssumeCapacity(&junk);
        for (source) |*slot| {
            const id = intern(&representatives, slot);
            source_ids.appendAssumeCapacity(id);
            deficits[id] -= 1;
        }
        for (target, 0..) |*slot, offset| {
            const id = intern(&representatives, slot);
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

fn intern(representatives: *std.ArrayList(*const CFG.StackSlot), slot: *const CFG.StackSlot) usize {
    for (representatives.items, 0..) |representative, id|
        if (representative.eql(slot.*)) return id;
    const id = representatives.items.len;
    representatives.appendAssumeCapacity(slot);
    return id;
}
