/*
 * Host test for the 1.6.0 batch drain of the flash queue (src/queue.h with
 * QUEUE_BATCH > 1). The 1.5.0 behaviour, one reading a request, is covered
 * by queue_host_test.cpp, which builds queue.h with batching off.
 *
 *   cd firmware/synthetic_wifi
 *   g++ -std=c++17 -Wall -I src -I test test/batch_host_test.cpp -o btest && ./btest
 */
#define QUEUE_BATCH 12

#include <deque>
#include <set>
#include <string>
#include <vector>

#include "arduino_shim.h"
#include "queue.h"

static int g_pass = 0, g_fail = 0;
static const char *g_case = "";
#define CHECK(c) do { if (c) g_pass++; else { g_fail++; printf("  FAIL [%s] %s:%d: %s\n", g_case, __FILE__, __LINE__, #c); } } while (0)
static void beginCase(const char *n) { g_case = n; printf("== %s\n", n); }

// ------------------------------------------------------------ fake Pi
struct Srv {
  bool link = true;
  std::deque<int> script;                 // forced codes for the next batch calls
  std::vector<std::string> batches;       // every batch body offered
  std::vector<int> stored;                // seq of every reading accepted, in order
  int singles = 0;
  unsigned long costMs = 0;
};
static Srv srv;

static int seqOf(const std::string &line) {
  size_t p = line.find("\"seq\":");
  return p == std::string::npos ? -1 : atoi(line.c_str() + p + 6);
}
/* Split a batch body into its objects; empty on anything malformed. */
static std::vector<std::string> items(const std::string &b) {
  std::vector<std::string> out;
  if (b.size() < 2 || b.front() != '[' || b.back() != ']') return {};
  int depth = 0; size_t start = 0; bool inStr = false;
  for (size_t i = 1; i + 1 < b.size(); i++) {
    char c = b[i];
    if (inStr) { if (c == '\\') i++; else if (c == '"') inStr = false; continue; }
    if (c == '"') inStr = true;
    else if (c == '{') { if (depth++ == 0) start = i; }
    else if (c == '}') { if (--depth == 0) out.push_back(b.substr(start, i - start + 1)); }
    else if (depth == 0 && c != ',') return {};
  }
  return depth == 0 ? out : std::vector<std::string>{};
}

int sendBatch(const String &body) {
  srv.batches.push_back(body.s);
  g_millis += srv.costMs;
  if (!srv.script.empty()) { int c = srv.script.front(); srv.script.pop_front(); if (c < 200 || c >= 300) return c; }
  auto its = items(body.s);
  if (its.empty()) return 400;
  for (auto &i : its) { if (i.find("\"queued\":1") == std::string::npos) return 422; srv.stored.push_back(seqOf(i)); }
  return 200;
}
int sendBody(const String &body, bool) {
  srv.singles++;
  if (items("[" + body.s + "]").size() != 1 || body.s.find("BAD") != std::string::npos) return 400;
  srv.stored.push_back(seqOf(body.s));
  return 200;
}
bool linkUp() { return srv.link; }
void queueIdle() {}

static std::string mk(int seq) {
  char b[512];
  snprintf(b, sizeof(b),
           "{\"device_id\":\"esp32-smoke-01\",\"firmware\":\"1.6.0\",\"boot\":7,\"up_s\":%d,\"seq\":%d,"
           "\"sampled_at\":%ld,\"sensors\":{\"bme280\":true,\"mlx90614\":false,\"ads1115\":true},"
           "\"air_temp_c\":24.31,\"humidity_pct\":99.9,\"pressure_hpa\":993.2,"
           "\"rain_analog\":812,\"rain_tips\":3,\"rssi\":-79,\"buffered_count\":%d}",
           seq * 300, seq, 1793000000L + seq * 300L, seq);
  return b;
}
static void fresh() {
  g_fs = FsState(); g_millis = 0; srv = Srv(); qBatch = QUEUE_BATCH;
  queueBegin();
}
static void push(int from, int to) { for (int i = from; i < to; i++) queuePush(String(mk(i))); }
static bool inOrder(int from, int to) {
  if ((int)srv.stored.size() != to - from) return false;
  for (int i = from; i < to; i++) if (srv.stored[i - from] != i) return false;
  return true;
}
static void reboot() { queueBegin(); }

int main() {
  beginCase("a backlog goes up twelve at a time, in order, each once");
  fresh(); push(0, 50);
  queueFlush();
  CHECK(inOrder(0, 50));
  CHECK(srv.batches.size() == 5);          // 12 + 12 + 12 + 12 + 2
  CHECK(qCount == 0 && srv.singles == 0);
  for (auto &b : srv.batches) CHECK(b.size() <= BATCH_MAX_BYTES);

  beginCase("eight batches a loop at most; the rest next loop");
  fresh(); push(0, 120);
  queueFlush();
  CHECK(srv.stored.size() == 96 && qCount == 24);
  queueFlush();
  CHECK(inOrder(0, 120) && qCount == 0);

  beginCase("qBatch from the Pi: 5 a request, and 1 falls back to one at a time");
  fresh(); push(0, 13); qBatch = 5;
  queueFlush();
  CHECK(inOrder(0, 13) && srv.batches.size() == 3);
  fresh(); push(0, 7); qBatch = 1;
  queueFlush();
  CHECK(inOrder(0, 7) && srv.batches.empty() && srv.singles == 7);

  beginCase("link down mid-drain: nothing lost, nothing sent twice");
  fresh(); push(0, 40);
  srv.script = {200, 200, -1};
  queueFlush();
  CHECK(srv.stored.size() == 24 && qCount == 16);
  queueFlush();
  CHECK(inOrder(0, 40) && qCount == 0);

  beginCase("cursor survives a reboot between batches");
  fresh(); push(0, 30);
  srv.script = {200, -1};
  queueFlush();
  reboot();
  CHECK(qCount == 18);
  queueFlush();
  CHECK(inOrder(0, 30) && qCount == 0);

  beginCase("a refused batch is re-sent one at a time; only the bad line is lost");
  fresh(); push(0, 5); queuePush(String("{\"device_id\":\"x\",\"seq\":999,\"BAD\":1}")); push(5, 10);
  srv.script = {400};
  queueFlush();                            // batch refused -> singles for this loop
  CHECK(srv.singles >= 11);
  std::set<int> got(srv.stored.begin(), srv.stored.end());
  bool all = true; for (int i = 0; i < 10; i++) all = all && got.count(i);
  CHECK(all && !got.count(999) && qCount == 0);

  beginCase("torn and blank lines ride along and are dropped");
  fresh(); push(0, 3);
  { File f = LittleFS.open(segPath(qTail), "a"); f.print("\n"); f.print("{\"device_id\":\"torn"); f.close(); }
  push(3, 6);                              // a new segment starts after the torn tail
  queueFlush();
  CHECK(inOrder(0, 6) && qCount == 0);
  for (auto &b : srv.batches) CHECK(!items(b).empty());

  beginCase("segments roll over and are deleted once sent");
  fresh(); push(0, 600);                   // ~3 segments of 64 KB
  uint32_t segs = qTail - qHead + 1;
  CHECK(segs >= 3);
  for (int i = 0; i < 10 && qCount > 0; i++) queueFlush();
  CHECK(inOrder(0, 600) && qCount == 0);
  int left = 0; for (auto &kv : g_fs.files) if (kv.first.size() > 3 && kv.first.compare(kv.first.size() - 3, 3, ".jl") == 0 && !kv.second.empty()) left++;
  CHECK(left == 0);

  beginCase("the 90 s budget holds with batches too");
  fresh(); push(0, 120); srv.costMs = 40000;
  queueFlush();
  CHECK(srv.batches.size() == 3 && qCount == 120 - 36);

  beginCase("no link, no tries");
  fresh(); push(0, 5); srv.link = false;
  queueFlush();
  CHECK(srv.batches.empty() && qCount == 5);

  if (getenv("BTEST_CANARY")) { beginCase("canary: must fail"); CHECK(1 == 2); }
  printf("\n%d checks passed, %d failed\n", g_pass, g_fail);
  return g_fail ? 1 : 0;
}
