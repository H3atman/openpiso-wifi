package routeros

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"html"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"
)

type Certificate struct {
	DER         []byte
	Fingerprint string
}

// ProbeCertificate reads only the Router's public certificate. No credentials or
// application data are sent before the operator confirms its fingerprint.
func ProbeCertificate(ctx context.Context, address string) (Certificate, error) {
	parsed, host, endpoint, err := parseAddress(address)
	if err != nil {
		return Certificate{}, err
	}

	dialer := &tls.Dialer{
		NetDialer: &net.Dialer{Timeout: 10 * time.Second},
		Config: &tls.Config{
			MinVersion: tls.VersionTLS12,
			ServerName: host,
		},
	}
	connection, err := dialer.DialContext(ctx, "tcp", endpoint)
	var peer []*x509.Certificate
	if err != nil {
		// A private/self-signed Router certificate fails normal trust validation.
		// Display the certificate from that failed handshake, never disable
		// verification or send credentials through an untrusted connection.
		var verificationError *tls.CertificateVerificationError
		if !errors.As(err, &verificationError) {
			return Certificate{}, fmt.Errorf("connect to Router %s: %w", parsed.Host, err)
		}
		peer = verificationError.UnverifiedCertificates
	} else {
		defer connection.Close()
		peer = connection.(*tls.Conn).ConnectionState().PeerCertificates
	}
	if len(peer) == 0 {
		return Certificate{}, errors.New("Router did not present a TLS certificate")
	}
	certificate := peer[0]
	if err := certificate.VerifyHostname(host); err != nil {
		return Certificate{}, fmt.Errorf("Router certificate is not valid for %s: %w", host, err)
	}
	now := time.Now()
	if now.Before(certificate.NotBefore) || now.After(certificate.NotAfter) {
		return Certificate{}, errors.New("Router certificate is not currently valid")
	}

	digest := sha256.Sum256(certificate.Raw)
	return Certificate{DER: certificate.Raw, Fingerprint: formatFingerprint(digest[:])}, nil
}

func parseAddress(address string) (*url.URL, string, string, error) {
	parsed, err := url.Parse(address)
	if err != nil {
		return nil, "", "", fmt.Errorf("parse Router address: %w", err)
	}
	if parsed.Scheme != "https" || parsed.Hostname() == "" || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return nil, "", "", errors.New("Router address must be an HTTPS origin such as https://192.0.2.1")
	}
	if parsed.Path != "" && parsed.Path != "/" {
		return nil, "", "", errors.New("Router address must not include a path")
	}
	host := parsed.Hostname()
	port := parsed.Port()
	if port == "" {
		port = "443"
	}
	return parsed, host, net.JoinHostPort(host, port), nil
}

func formatFingerprint(digest []byte) string {
	encoded := strings.ToUpper(hex.EncodeToString(digest))
	parts := make([]string, 0, len(encoded)/2)
	for index := 0; index < len(encoded); index += 2 {
		parts = append(parts, encoded[index:index+2])
	}
	return "SHA256:" + strings.Join(parts, ":")
}

func certPool(certificateDER []byte) (*x509.CertPool, error) {
	certificate, err := x509.ParseCertificate(certificateDER)
	if err != nil {
		return nil, fmt.Errorf("parse pinned Router certificate: %w", err)
	}
	pool := x509.NewCertPool()
	pool.AddCert(certificate)
	return pool, nil
}

type Client struct {
	baseURL     *url.URL
	username    string
	password    string
	certificate Certificate
	http        *http.Client
}

func NewClient(address, username, password string, certificate Certificate) (*Client, error) {
	parsed, host, _, err := parseAddress(address)
	if err != nil {
		return nil, err
	}
	roots, err := certPool(certificate.DER)
	if err != nil {
		return nil, err
	}
	tlsConfig := &tls.Config{
		MinVersion: tls.VersionTLS12,
		ServerName: host,
		RootCAs:    roots,
		VerifyConnection: func(state tls.ConnectionState) error {
			if len(state.PeerCertificates) == 0 || !bytes.Equal(state.PeerCertificates[0].Raw, certificate.DER) {
				return errors.New("Router certificate fingerprint mismatch")
			}
			return nil
		},
	}
	return &Client{
		baseURL:     parsed,
		username:    username,
		password:    password,
		certificate: certificate,
		http: &http.Client{
			Timeout: 20 * time.Second,
			CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
				return errors.New("Router REST redirects are not allowed")
			},
			Transport: &http.Transport{
				Proxy:           nil,
				TLSClientConfig: tlsConfig,
			},
		},
	}, nil
}

// Close releases idle connections when a short-lived Router operation finishes.
func (c *Client) Close() {
	c.http.CloseIdleConnections()
}

type routerError struct {
	Detail string `json:"detail"`
	Error  int    `json:"error"`
}

func (c *Client) request(ctx context.Context, method, path string, body any, result any) error {
	var payload io.Reader
	if body != nil {
		encoded, err := json.Marshal(body)
		if err != nil {
			return fmt.Errorf("encode Router request: %w", err)
		}
		payload = bytes.NewReader(encoded)
	}
	endpoint := *c.baseURL
	endpoint.Path = "/rest" + path
	endpoint.RawQuery = ""
	if queryAt := strings.IndexByte(path, '?'); queryAt >= 0 {
		endpoint.Path = "/rest" + path[:queryAt]
		endpoint.RawQuery = path[queryAt+1:]
	}
	// RouterOS REST identifies resources with values such as *4 and does not
	// accept net/url's escaped %2A form.
	endpoint.RawPath = endpoint.Path
	request, err := http.NewRequestWithContext(ctx, method, endpoint.String(), payload)
	if err != nil {
		return fmt.Errorf("create Router request: %w", err)
	}
	request.SetBasicAuth(c.username, c.password)
	if body != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	response, err := c.http.Do(request)
	if err != nil {
		return fmt.Errorf("Router HTTPS request failed: %w", err)
	}
	defer response.Body.Close()
	responseBody, err := io.ReadAll(io.LimitReader(response.Body, 4<<20))
	if err != nil {
		return fmt.Errorf("read Router response: %w", err)
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		var routerResponse routerError
		_ = json.Unmarshal(responseBody, &routerResponse)
		if routerResponse.Detail != "" {
			return fmt.Errorf("Router rejected %s %s: %s", method, path, routerResponse.Detail)
		}
		return fmt.Errorf("Router rejected %s %s with HTTP %d", method, path, response.StatusCode)
	}
	if result != nil && len(responseBody) > 0 {
		if err := json.Unmarshal(responseBody, result); err != nil {
			return fmt.Errorf("decode Router response: %w", err)
		}
	}
	return nil
}

func (c *Client) list(ctx context.Context, path string) ([]map[string]string, error) {
	var result []map[string]string
	if err := c.request(ctx, http.MethodGet, path, nil, &result); err != nil {
		return nil, err
	}
	return result, nil
}

func (c *Client) ensureNamed(ctx context.Context, path, name string, values map[string]string) error {
	items, err := c.list(ctx, path+"?name="+url.QueryEscape(name))
	if err != nil {
		return err
	}
	values["name"] = name
	if len(items) == 0 {
		return c.request(ctx, http.MethodPut, path, values, nil)
	}
	delete(values, "name")
	return c.updateChanged(ctx, path, items[0], values)
}

func (c *Client) updateChanged(ctx context.Context, path string, current, desired map[string]string) error {
	changes := make(map[string]string)
	for key, value := range desired {
		actual := current[key]
		matches := actual == value || (value == "no" && actual == "false") || (value == "yes" && actual == "true")
		if !matches && strings.HasSuffix(key, "-timeout") {
			// RouterOS reads a written "120s" back as "2m".
			a, errA := durationSeconds(actual)
			b, errB := durationSeconds(value)
			matches = errA == nil && errB == nil && a == b
		}
		if key == "policy" {
			matches = true
			for _, policy := range strings.Split(value, ",") {
				if !strings.Contains(","+actual+",", ","+policy+",") {
					matches = false
				}
			}
			for _, policy := range strings.Split(actual, ",") {
				if !strings.HasPrefix(policy, "!") && !strings.Contains(","+value+",", ","+policy+",") {
					matches = false
				}
			}
		}
		if !matches {
			changes[key] = value
		}
	}
	if len(changes) == 0 {
		return nil
	}
	return c.request(ctx, http.MethodPatch, path+"/"+current[".id"], changes, nil)
}

func (c *Client) systemVersion(ctx context.Context) (string, error) {
	var resource map[string]string
	if err := c.request(ctx, http.MethodGet, "/system/resource", nil, &resource); err != nil {
		return "", err
	}
	version := resource["version"]
	if !strings.HasPrefix(version, "7.") {
		return "", fmt.Errorf("RouterOS v7 is required; Router reports %q", version)
	}
	return version, nil
}

const (
	controllerUsername = "openpiso-controller"
	controllerGroup    = "openpiso-controller"
	controllerPolicy   = "read,write,test,api,rest-api"
)

type Provisioned struct {
	Address     string
	Username    string
	APISecret   string
	Certificate Certificate
	Version     string
}

func Onboard(ctx context.Context, admin *Client, portalURL, existingSecret string) (Provisioned, error) {
	version, err := admin.systemVersion(ctx)
	if err != nil {
		return Provisioned{}, err
	}
	if err := admin.ensureNamed(ctx, "/user/group", controllerGroup, map[string]string{
		"policy": controllerPolicy,
	}); err != nil {
		return Provisioned{}, fmt.Errorf("create limited Router group: %w", err)
	}
	secret := existingSecret
	userValues := map[string]string{"group": controllerGroup}
	if secret == "" {
		secretBytes := make([]byte, 32)
		if _, err := rand.Read(secretBytes); err != nil {
			return Provisioned{}, fmt.Errorf("generate Router API secret: %w", err)
		}
		secret = base64.RawURLEncoding.EncodeToString(secretBytes)
		userValues["password"] = secret
	}
	users, err := admin.list(ctx, "/user?name="+controllerUsername)
	if err != nil {
		return Provisioned{}, err
	}
	if len(users) == 0 {
		userValues["password"] = secret
	}
	if err := admin.ensureNamed(ctx, "/user", controllerUsername, userValues); err != nil {
		return Provisioned{}, fmt.Errorf("create limited Router user: %w", err)
	}
	limited, err := NewClient(admin.baseURL.String(), controllerUsername, secret, admin.certificate)
	if err != nil {
		return Provisioned{}, err
	}
	if _, err := limited.systemVersion(ctx); err != nil {
		return Provisioned{}, fmt.Errorf("verify limited Router user: %w", err)
	}
	if err := limited.EnsureBaseline(ctx, portalURL); err != nil {
		return Provisioned{}, err
	}
	return Provisioned{
		Address:     admin.baseURL.String(),
		Username:    controllerUsername,
		APISecret:   secret,
		Certificate: admin.certificate,
		Version:     version,
	}, nil
}

func (c *Client) EnsureBaseline(ctx context.Context, portalURL string, idlePeriod ...int) error {
	if _, err := c.systemVersion(ctx); err != nil {
		return err
	}
	idleSeconds := 120
	if len(idlePeriod) > 0 {
		idleSeconds = idlePeriod[0]
	}
	if err := c.ensureProfilePair(ctx, "", nil, idleSeconds); err != nil {
		return err
	}

	hotspots, err := c.list(ctx, "/ip/hotspot")
	if err != nil {
		return fmt.Errorf("list HotSpot servers: %w", err)
	}
	seenProfiles := make(map[string]struct{})
	for _, hotspot := range hotspots {
		name := hotspot["profile"]
		if name == "" {
			continue
		}
		if _, seen := seenProfiles[name]; seen {
			continue
		}
		seenProfiles[name] = struct{}{}
		if err := c.ensureNamed(ctx, "/ip/hotspot/profile", name, map[string]string{
			"login-by":       "http-pap",
			"html-directory": "hotspot",
		}); err != nil {
			return fmt.Errorf("ensure HotSpot server profile %s: %w", name, err)
		}
	}
	if len(seenProfiles) == 0 {
		return errors.New("Router has no HotSpot server to configure")
	}

	portal, err := url.Parse(portalURL)
	if err != nil || portal.Hostname() == "" || (portal.Scheme != "http" && portal.Scheme != "https") {
		return errors.New("portal URL must be an HTTP or HTTPS origin")
	}
	port := portal.Port()
	if port == "" {
		if portal.Scheme == "https" {
			port = "443"
		} else {
			port = "80"
		}
	}
	if net.ParseIP(portal.Hostname()) == nil {
		return errors.New("the walking skeleton requires an IP address in the portal URL")
	}
	if err := c.ensureByComment(ctx, "/ip/hotspot/walled-garden/ip", "openpiso-controller", map[string]string{
		"action":      "accept",
		"dst-address": portal.Hostname(),
		"dst-port":    port,
		"protocol":    "tcp",
	}); err != nil {
		return fmt.Errorf("ensure Controller walled garden: %w", err)
	}

	portalOrigin := html.EscapeString(strings.TrimRight(portalURL, "/"))
	loginHTML := fmt.Sprintf(`<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>OpenPiso WiFi</title></head>
<body><main><h1>OpenPiso WiFi</h1><p>Service temporarily unavailable. Please try again shortly.</p><p><a href="$(link-login-only)">Try again</a></p>
<noscript><p>Enable JavaScript to continue when the Controller is available.</p></noscript></main>
<script src="%s/portal/ready.js" data-portal="%s/portal?mac=$(mac-esc)&amp;ip=$(ip)" onload="location.replace(this.dataset.portal)"></script></body></html>`, portalOrigin, portalOrigin)
	if err := c.ensureFile(ctx, "hotspot/login.html", loginHTML); err != nil {
		return fmt.Errorf("ensure HotSpot login redirect: %w", err)
	}
	return nil
}

func (c *Client) ensureByComment(ctx context.Context, path, comment string, values map[string]string) error {
	items, err := c.list(ctx, path+"?comment="+url.QueryEscape(comment))
	if err != nil {
		return err
	}
	values["comment"] = comment
	if len(items) == 0 {
		return c.request(ctx, http.MethodPut, path, values, nil)
	}
	delete(values, "comment")
	return c.updateChanged(ctx, path, items[0], values)
}

func (c *Client) ensureFile(ctx context.Context, name, contents string) error {
	items, err := c.list(ctx, "/file?name="+url.QueryEscape(name))
	if err != nil {
		return err
	}
	if len(items) == 0 {
		return c.request(ctx, http.MethodPut, "/file", map[string]string{"name": name, "contents": contents}, nil)
	}
	return c.updateChanged(ctx, "/file", items[0], map[string]string{"contents": contents})
}
