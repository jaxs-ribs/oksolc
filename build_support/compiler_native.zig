// SPDX-License-Identifier: GPL-3.0
// Copyright (C) 2026 OKcontract Pte. Ltd.

const std = @import("std");

/// Share native objects across compiler artifacts and build them alongside C
/// translation. Each source is a build job, subject to the caller's -j limit.
pub fn link(
    b: *std.Build,
    compiler: *std.Build.Module,
    yyjson_source: std.Build.LazyPath,
    identity: std.Build.LazyPath,
    extra_flags: []const []const u8,
) void {
    const sources = [_]struct {
        name: []const u8,
        file: std.Build.LazyPath,
        flags: []const []const u8,
    }{
        .{
            .name = "oksolc-yyjson-adapter",
            .file = b.path("src/libsolc/yyjson_adapter.c"),
            .flags = &.{ "-std=c99", "-fvisibility=hidden", "-Dyyjson_api=" },
        },
        .{
            .name = "oksolc-yyjson",
            .file = yyjson_source.path(b, "yyjson.c"),
            .flags = &.{ "-std=c99", "-fvisibility=hidden", "-Dyyjson_api=" },
        },
        .{
            .name = "oksolc-sqlite",
            .file = b.path("vendor/sqlite/sqlite3.c"),
            .flags = &.{
                "-std=c99",
                "-fvisibility=hidden",
                "-DSQLITE_THREADSAFE=1",
                "-DSQLITE_DQS=0",
                "-DSQLITE_DEFAULT_MEMSTATUS=0",
                "-DSQLITE_DEFAULT_WAL_SYNCHRONOUS=1",
                "-DSQLITE_OMIT_DEPRECATED",
                "-DSQLITE_OMIT_LOAD_EXTENSION",
                "-DSQLITE_TEMP_STORE=3",
            },
        },
    };
    for (sources) |source| {
        const module = b.createModule(.{
            .target = compiler.resolved_target,
            .optimize = compiler.optimize,
            .link_libc = true,
            // The same object also links into the shared libsolc library.
            .pic = true,
        });
        module.addIncludePath(b.path("include"));
        module.addIncludePath(yyjson_source);
        module.addIncludePath(b.path("vendor/sqlite"));
        module.addCSourceFile(.{
            .file = source.file,
            .flags = std.mem.concat(b.allocator, []const u8, &.{ source.flags, extra_flags }) catch @panic("OOM"),
        });
        const object = b.addObject(.{ .name = source.name, .root_module = module });
        // Hash before C reads working-tree inputs, just as for translation.
        identity.addStepDependencies(&object.step);
        compiler.addObject(object);
    }
}
