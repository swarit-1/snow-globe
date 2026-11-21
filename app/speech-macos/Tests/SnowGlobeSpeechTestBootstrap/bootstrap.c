// Runs once when the test bundle is loaded, before XCTest starts any test (a Mach-O
// initializer). It puts the trap engine and the fake status probe behind the library's seams for
// the whole process, so a test that forgets to install a mock, in any class or in a filtered
// run, gets an ERROR event and never the real recognizer, which could show a permission prompt
// (README, "What the tests need"). The per-test resets in SpeechTestCase stay as well.

#include "snow_globe_speech_test_bootstrap.h"

__attribute__((constructor)) static void sg_speech_tests_bootstrap(void) {
    sg_speech_tests_install_trap();
}
