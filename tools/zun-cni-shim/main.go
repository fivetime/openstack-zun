// zun-cni is the CNI plugin binary a container runtime executes on the host.
//
// It does no networking itself. It forwards the call -- the CNI_* environment
// and the network configuration on stdin -- to zun-cni-daemon's /cni endpoint
// and prints what comes back. The daemon plugs the port and turns the result
// into CNI's own format, so this binary needs nothing on the host: no Python,
// no libraries. That is the point of it. The Python plugin (zun.cni.cmd.cni)
// does the same job but only runs where zun's Python environment is installed,
// which a host whose daemon runs in a container does not have.
//
// The daemon's address comes from the network configuration's
// "zun_cni_daemon" key, and defaults to the daemon's default listen address.
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"
)

const (
	cniVersion    = "0.3.1"
	defaultDaemon = "http://127.0.0.1:9036"
	// Error codes the daemon also uses (zun.common.consts).
	codeException = 100
	// The daemon waits up to vif_active_timeout for a port to become active;
	// leave it room to answer before giving up here.
	requestTimeout = 170 * time.Second
)

type netConf struct {
	Daemon string `json:"zun_cni_daemon"`
}

func main() {
	client := &http.Client{Timeout: requestTimeout}
	os.Exit(run(os.Environ(), os.Stdin, os.Stdout, client))
}

func run(environ []string, stdin io.Reader, stdout io.Writer, client *http.Client) int {
	env := map[string]interface{}{}
	command := ""
	for _, kv := range environ {
		k, v, ok := strings.Cut(kv, "=")
		if !ok || !strings.HasPrefix(k, "CNI_") {
			continue
		}
		env[k] = v
		if k == "CNI_COMMAND" {
			command = v
		}
	}

	// VERSION is answered here: it must work before any daemon exists.
	if command == "VERSION" {
		return emit(stdout, map[string]interface{}{
			"cniVersion":        cniVersion,
			"supportedVersions": []string{cniVersion},
		}, 0)
	}

	raw, err := io.ReadAll(stdin)
	if err != nil {
		return fail(stdout, "reading the network configuration: "+err.Error())
	}
	var conf netConf
	if len(bytes.TrimSpace(raw)) == 0 || json.Unmarshal(raw, &conf) != nil {
		return fail(stdout, "the network configuration on stdin is not JSON")
	}
	daemon := strings.TrimRight(conf.Daemon, "/")
	if daemon == "" {
		daemon = defaultDaemon
	}

	env["config_zun"] = json.RawMessage(raw)
	body, err := json.Marshal(env)
	if err != nil {
		return fail(stdout, "encoding the request: "+err.Error())
	}

	resp, err := client.Post(daemon+"/cni", "application/json", bytes.NewReader(body))
	if err != nil {
		return fail(stdout, fmt.Sprintf("zun-cni-daemon at %s cannot be reached: %v", daemon, err))
	}
	defer resp.Body.Close()
	out, err := io.ReadAll(resp.Body)
	if err != nil {
		return fail(stdout, "reading the daemon's answer: "+err.Error())
	}

	if resp.StatusCode == http.StatusOK {
		// DEL answers with nothing, and CNI wants nothing printed for it.
		if command != "DEL" && len(bytes.TrimSpace(out)) > 0 {
			stdout.Write(out)
		}
		return 0
	}
	// The daemon's errors are already CNI errors; pass them on as they are.
	var errObj map[string]interface{}
	if json.Unmarshal(out, &errObj) == nil && errObj["code"] != nil {
		stdout.Write(out)
		return 1
	}
	return fail(stdout, fmt.Sprintf("zun-cni-daemon answered %d: %s", resp.StatusCode, strings.TrimSpace(string(out))))
}

func fail(stdout io.Writer, msg string) int {
	return emit(stdout, map[string]interface{}{
		"cniVersion": cniVersion,
		"code":       codeException,
		"msg":        msg,
	}, 1)
}

func emit(stdout io.Writer, v map[string]interface{}, status int) int {
	enc := json.NewEncoder(stdout)
	enc.Encode(v)
	return status
}
