//! Build script for `m1-sck`. The `screencapturekit` crate links a Swift bridge whose binaries
//! load `@rpath/libswift_Concurrency.dylib`, but a dependency's build script cannot add an rpath
//! to this crate's binaries. Without this line the helper (and its unit tests) abort at launch
//! with "Library not loaded: @rpath/libswift_Concurrency.dylib" (seen on 19 Nov 2026). The Swift
//! runtime lives in the OS at /usr/lib/swift (dyld shared cache).

fn main() {
    println!("cargo:rustc-link-arg=-Wl,-rpath,/usr/lib/swift");
}
