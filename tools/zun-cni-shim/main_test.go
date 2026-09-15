package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

type seen struct {
	path string
	body map[string]json.RawMessage
}

func daemon(t *testing.T, status int, answer string, got *seen) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got.path = r.URL.Path
		raw, _ := io.ReadAll(r.Body)
		if err := json.Unmarshal(raw, &got.body); err != nil {
			t.Errorf("request body is not JSON: %s", raw)
		}
		w.WriteHeader(status)
		io.WriteString(w, answer)
	}))
}

func env(command string) []string {
	return []string{
		"CNI_COMMAND=" + command, "CNI_CONTAINERID=sb-1", "CNI_NETNS=/var/run/netns/cni-1",
		"CNI_IFNAME=eth0", "CNI_ARGS=K8S_POD_NAME=capsule-1;IgnoreUnknown=1",
		"CNI_PATH=/opt/cni/bin", "PATH=/usr/bin", "HOME=/root",
	}
}

func conf(url string) string {
	return `{"cniVersion":"0.3.1","name":"zun","type":"zun-cni","zun_cni_daemon":"` + url + `"}`
}

func TestAddForwardsTheCallAndPrintsTheResult(t *testing.T) {
	var got seen
	result := `{"cniVersion":"0.3.1","ips":[{"address":"192.168.1.30/24"}]}`
	srv := daemon(t, 200, result, &got)
	defer srv.Close()
	var out bytes.Buffer

	rc := run(env("ADD"), strings.NewReader(conf(srv.URL)), &out, srv.Client())

	if rc != 0 {
		t.Fatalf("exit %d, output %s", rc, out.String())
	}
	if out.String() != result {
		t.Errorf("printed %q, want the daemon's result verbatim", out.String())
	}
	if got.path != "/cni" {
		t.Errorf("posted to %s", got.path)
	}
	for _, k := range []string{"CNI_COMMAND", "CNI_CONTAINERID", "CNI_NETNS", "CNI_IFNAME", "CNI_ARGS", "CNI_PATH"} {
		if _, ok := got.body[k]; !ok {
			t.Errorf("%s not forwarded", k)
		}
	}
	if _, ok := got.body["PATH"]; ok {
		t.Errorf("non-CNI environment forwarded")
	}
	var cfg map[string]string
	json.Unmarshal(got.body["config_zun"], &cfg)
	if cfg["type"] != "zun-cni" {
		t.Errorf("network configuration not forwarded as config_zun: %s", got.body["config_zun"])
	}
}

func TestDelPrintsNothing(t *testing.T) {
	var got seen
	srv := daemon(t, 200, "", &got)
	defer srv.Close()
	var out bytes.Buffer

	rc := run(env("DEL"), strings.NewReader(conf(srv.URL)), &out, srv.Client())

	if rc != 0 || out.Len() != 0 {
		t.Errorf("exit %d, output %q; want 0 and nothing", rc, out.String())
	}
}

func TestVersionNeedsNoDaemon(t *testing.T) {
	var out bytes.Buffer

	rc := run([]string{"CNI_COMMAND=VERSION"}, strings.NewReader(""), &out, http.DefaultClient)

	if rc != 0 || !strings.Contains(out.String(), `"supportedVersions":["0.3.1"]`) {
		t.Errorf("exit %d, output %s", rc, out.String())
	}
}

func TestADaemonErrorIsPassedOnAndFails(t *testing.T) {
	var got seen
	cniErr := `{"cniVersion":"0.3.1","code":200,"msg":"the port did not become active in time"}`
	srv := daemon(t, 504, cniErr, &got)
	defer srv.Close()
	var out bytes.Buffer

	rc := run(env("ADD"), strings.NewReader(conf(srv.URL)), &out, srv.Client())

	if rc != 1 || out.String() != cniErr {
		t.Errorf("exit %d, output %s", rc, out.String())
	}
}

func TestAnUnreachableDaemonIsACNIError(t *testing.T) {
	srv := httptest.NewServer(http.NotFoundHandler())
	url := srv.URL
	srv.Close()
	var out bytes.Buffer

	rc := run(env("ADD"), strings.NewReader(conf(url)), &out, http.DefaultClient)

	var e map[string]interface{}
	json.Unmarshal(out.Bytes(), &e)
	if rc != 1 || e["code"] != float64(100) || !strings.Contains(e["msg"].(string), "cannot be reached") {
		t.Errorf("exit %d, output %s", rc, out.String())
	}
}

func TestANonCNIErrorBodyStillBecomesACNIError(t *testing.T) {
	var got seen
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(502)
		io.WriteString(w, "<html>bad gateway</html>")
	}))
	defer srv.Close()
	_ = got
	var out bytes.Buffer

	rc := run(env("ADD"), strings.NewReader(conf(srv.URL)), &out, srv.Client())

	var e map[string]interface{}
	if rc != 1 || json.Unmarshal(out.Bytes(), &e) != nil || e["code"] != float64(100) {
		t.Errorf("exit %d, output %s", rc, out.String())
	}
}

func TestGarbageOnStdinIsRefusedWithoutCallingTheDaemon(t *testing.T) {
	var out bytes.Buffer

	rc := run(env("ADD"), strings.NewReader("not json"), &out, http.DefaultClient)

	if rc != 1 || !strings.Contains(out.String(), "not JSON") {
		t.Errorf("exit %d, output %s", rc, out.String())
	}
}

func TestTheDefaultDaemonAddressIsTheDaemonsDefault(t *testing.T) {
	if defaultDaemon != "http://127.0.0.1:9036" {
		t.Errorf("default %s drifted from [cni_daemon] cni_daemon_host/port defaults", defaultDaemon)
	}
}
