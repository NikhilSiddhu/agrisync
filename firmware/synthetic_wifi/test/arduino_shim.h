#pragma once
/*
 * Just enough of Arduino and LittleFS to run src/queue.h on a computer.
 *
 * The filesystem lives in memory and counts space the way LittleFS does on
 * the board's 1.4 MB partition: whole 4 KB blocks, two of them for its own
 * metadata, so the queue's "flash is short" paths can be exercised.
 */
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <set>
#include <string>
#include <vector>

class String {
 public:
  std::string s;
  String() {}
  String(const char *c) : s(c ? c : "") {}
  String(const std::string &x) : s(x) {}
  unsigned int length() const { return (unsigned int)s.size(); }
  const char *c_str() const { return s.c_str(); }
  void trim() {
    size_t a = s.find_first_not_of(" \t\r\n"), b = s.find_last_not_of(" \t\r\n");
    s = (a == std::string::npos) ? "" : s.substr(a, b - a + 1);
  }
  bool endsWith(const char *x) const {
    size_t n = strlen(x);
    return s.size() >= n && s.compare(s.size() - n, n, x) == 0;
  }
  int indexOf(const char *x) const { size_t p = s.find(x); return p == std::string::npos ? -1 : (int)p; }
  int indexOf(char c) const { size_t p = s.find(c); return p == std::string::npos ? -1 : (int)p; }
  int lastIndexOf(char c) const { size_t p = s.rfind(c); return p == std::string::npos ? -1 : (int)p; }
  void remove(unsigned int i) { if (i < s.size()) s.erase(i); }
  String substring(unsigned int a) const { return a < s.size() ? String(s.substr(a)) : String(); }
  String substring(unsigned int a, unsigned int b) const { return a < s.size() ? String(s.substr(a, b - a)) : String(); }
  long toInt() const { return atol(s.c_str()); }
  char operator[](unsigned int i) const { return i < s.size() ? s[i] : 0; }
  String &operator+=(const char *x) { s += x; return *this; }
  String &operator+=(const String &x) { s += x.s; return *this; }
};

static unsigned long g_millis = 0;
inline unsigned long millis() { return g_millis; }
inline void delay(unsigned long ms) { g_millis += ms; }

struct SerialShim {
  std::string out;                      // everything the board printed, for the tests to read
  void printf(const char *fmt, ...) {
    char buf[1024];
    va_list ap; va_start(ap, fmt); int n = vsnprintf(buf, sizeof(buf), fmt, ap); va_end(ap);
    if (n > 0) out.append(buf, std::min((size_t)n, sizeof(buf) - 1));
  }
  void println(const char *x = "") { out += x; out += "\n"; }
  void println(const String &x) { println(x.c_str()); }
  void print(const char *x) { out += x; }
  void print(const String &x) { print(x.c_str()); }
};
static SerialShim Serial;

// ---------------------------------------------------------------- files
struct FsState {
  std::map<std::string, std::string> files;
  std::set<std::string> dirs;
  size_t total = 0x160000;              // the default partition on an esp32dev
  size_t blockSize = 4096;
  int failWritesAfter = -1;             // >= 0: bytes still writable before "flash full"
};
static FsState g_fs;

class File {
 public:
  std::string path;
  std::string mode;
  size_t pos = 0;
  bool open = false, dir = false;
  std::vector<std::string> listing;
  size_t listIndex = 0;

  operator bool() const { return open; }
  std::string &data() { return g_fs.files[path]; }
  size_t size() { return open && !dir ? data().size() : 0; }
  bool seek(size_t p) { pos = p; return true; }
  size_t position() const { return pos; }
  int available() { return open && !dir && pos < data().size() ? (int)(data().size() - pos) : 0; }
  int read(uint8_t *buf, size_t n) {
    if (!available()) return 0;
    size_t k = std::min(n, data().size() - pos);
    memcpy(buf, data().data() + pos, k);
    pos += k;
    return (int)k;
  }
  String readStringUntil(char term) {
    std::string out;
    while (available()) {
      char c = data()[pos++];
      if (c == term) break;
      out += c;
    }
    return String(out);
  }
  size_t write(const char *p, size_t n) {
    if (!open || mode == "r") return 0;
    if (g_fs.failWritesAfter >= 0) {
      size_t k = std::min(n, (size_t)g_fs.failWritesAfter);
      g_fs.failWritesAfter -= (int)k;
      n = k;
    }
    std::string &d = data();
    if (mode == "a") pos = d.size();
    if (pos > d.size()) d.resize(pos);
    d.replace(pos, std::min(n, d.size() - pos), p, n);
    pos += n;
    return n;
  }
  size_t print(const String &x) { return write(x.s.data(), x.s.size()); }
  size_t print(const char *x) { return write(x, strlen(x)); }
  size_t print(char c) { return write(&c, 1); }
  size_t printf(const char *fmt, ...) {
    char buf[256];
    va_list ap; va_start(ap, fmt); int n = vsnprintf(buf, sizeof(buf), fmt, ap); va_end(ap);
    return write(buf, n > 0 ? (size_t)n : 0);
  }
  void close() { open = false; }
  const char *name() const { size_t sl = path.rfind('/'); return path.c_str() + (sl == std::string::npos ? 0 : sl + 1); }
  bool isDirectory() const { return dir; }
  File openNextFile();
};

class LittleFSShim {
 public:
  bool begin(bool) { return true; }
  bool exists(const char *p) { return g_fs.files.count(p) || g_fs.dirs.count(p); }
  bool exists(const String &p) { return exists(p.c_str()); }
  File open(const char *p, const char *mode = "r") {
    File f;
    std::string path(p);
    if (g_fs.dirs.count(path)) {
      f.path = path; f.open = true; f.dir = true; f.mode = "r";
      std::string pre = path + "/";
      for (auto &kv : g_fs.files) {
        if (kv.first.compare(0, pre.size(), pre) == 0 && kv.first.find('/', pre.size()) == std::string::npos)
          f.listing.push_back(kv.first);
      }
      return f;
    }
    std::string m(mode);
    if (m == "r" && !g_fs.files.count(path)) return f;
    if (m == "w") g_fs.files[path] = "";
    if (m == "a" && !g_fs.files.count(path)) g_fs.files[path] = "";
    f.path = path; f.mode = m; f.open = true; f.pos = 0;
    return f;
  }
  File open(const String &p, const char *mode = "r") { return open(p.c_str(), mode); }
  bool remove(const char *p) { return g_fs.files.erase(p) > 0; }
  bool remove(const String &p) { return remove(p.c_str()); }
  bool rename(const char *a, const char *b) {
    if (!g_fs.files.count(a)) return false;
    g_fs.files[b] = g_fs.files[a];
    g_fs.files.erase(a);
    return true;
  }
  bool mkdir(const char *p) { g_fs.dirs.insert(p); return true; }
  size_t totalBytes() { return g_fs.total; }
  size_t usedBytes() {
    size_t blocks = 2;                                  // superblock and root directory
    for (auto &kv : g_fs.files) blocks += (kv.second.size() + g_fs.blockSize - 1) / g_fs.blockSize + 0;
    return blocks * g_fs.blockSize;
  }
};
static LittleFSShim LittleFS;

inline File File::openNextFile() {
  File f;
  if (!dir || listIndex >= listing.size()) return f;
  f.path = listing[listIndex++];
  f.open = true; f.mode = "r";
  return f;
}
