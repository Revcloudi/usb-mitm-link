package com.dbg;

import java.io.*;
import java.net.*;
import java.util.concurrent.*;

/**
 * MiniProxy —— 手机端纯转发 HTTP 代理（支持 CONNECT 隧道 + 普通 HTTP）
 * 出站走手机默认路由（本机 tun0 = 联软 VPN 白名单出口）
 * 不占用 VpnService，与联软 VPN 共存
 */
public class MiniProxy {

    public static void main(String[] args) throws Exception {
        int port = args.length > 0 ? Integer.parseInt(args[0]) : 17890;
        ServerSocket ss = new ServerSocket();
        ss.setReuseAddress(true);
        // 明确绑定 IPv4 回环：adb forward 从 adbd 侧连过来，IPv6 wildcard 会连不通
        ss.bind(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), port));
        System.out.println("[MiniProxy] bound to " + ss.getLocalSocketAddress() + " on 0.0.0.0:" + port);
        System.out.flush();
        ExecutorService pool = Executors.newCachedThreadPool();
        while (true) {
            final Socket c = ss.accept();
            try {
                c.setTcpNoDelay(true);
            } catch (Exception ignored) {}
            pool.submit(new Runnable() {
                public void run() { handle(c); }
            });
        }
    }

    static void handle(Socket client) {
        Socket upstream = null;
        try {
            client.setSoTimeout(60000);
            InputStream cin = client.getInputStream();
            BufferedInputStream bin = new BufferedInputStream(cin, 16384);

            String head = readHeaders(bin);
            if (head == null) { client.close(); return; }
            int eol = head.indexOf("\r\n");
            String requestLine = eol > 0 ? head.substring(0, eol) : head;
            String[] parts = requestLine.split(" ");
            if (parts.length < 2) { client.close(); return; }
            String method = parts[0];
            String target = parts[1];
            System.out.println("[req] " + requestLine);
            System.out.flush();

            OutputStream cout = client.getOutputStream();

            if ("CONNECT".equalsIgnoreCase(method)) {
                String host; int p;
                int colon = target.lastIndexOf(':');
                if (colon > 0) { host = target.substring(0, colon); p = Integer.parseInt(target.substring(colon + 1)); }
                else { host = target; p = 443; }
                upstream = new Socket();
                upstream.setTcpNoDelay(true);
                upstream.connect(new InetSocketAddress(host, p), 20000);
                cout.write("HTTP/1.1 200 Connection Established\r\n\r\n".getBytes("ISO-8859-1"));
                cout.flush();
                pipe(bin, upstream.getOutputStream());
                pipe(upstream.getInputStream(), cout);
            } else {
                URL u = new URL(target);
                int p = u.getPort() > 0 ? u.getPort() : 80;
                upstream = new Socket();
                upstream.setTcpNoDelay(true);
                upstream.connect(new InetSocketAddress(u.getHost(), p), 20000);
                OutputStream uos = upstream.getOutputStream();
                String path = u.getFile();
                if (path == null || path.isEmpty()) path = "/";
                StringBuilder sb = new StringBuilder();
                sb.append(method).append(' ').append(path).append(" HTTP/1.1\r\n");
                String rest = eol > 0 ? head.substring(eol + 2) : "";
                sb.append(rest);
                uos.write(sb.toString().getBytes("ISO-8859-1"));
                uos.flush();
                pipe(bin, uos);
                pipe(upstream.getInputStream(), cout);
            }
        } catch (Exception e) {
            System.out.println("[err] " + e);
            System.out.flush();
            try { client.close(); } catch (Exception ignored) {}
            if (upstream != null) { try { upstream.close(); } catch (Exception ignored) {} }
        }
    }

    /** 读到 \r\n\r\n 为止，返回含结尾空行的完整头部（ISO-8859-1 保留原始字节） */
    static String readHeaders(InputStream in) throws IOException {
        ByteArrayOutputStream bos = new ByteArrayOutputStream(2048);
        int h0 = 0, h1 = 0, h2 = 0, h3 = 0, b;
        while ((b = in.read()) != -1) {
            bos.write(b);
            h3 = h2; h2 = h1; h1 = h0; h0 = b;
            // 头部结束：\r\n\r\n ；宽松兼容 \n\n
            if (h3 == '\r' && h2 == '\n' && h1 == '\r' && h0 == '\n') break;
            if (h3 == '\n' && h2 == '\n' && h1 == '\n' && h0 == '\n') break;
            if (bos.size() > 262144) break;
        }
        if (bos.size() == 0) return null;
        return new String(bos.toByteArray(), "ISO-8859-1");
    }

    static void pipe(final InputStream in, final OutputStream out) {
        Thread t = new Thread(new Runnable() {
            public void run() {
                byte[] buf = new byte[32768];
                try {
                    int n;
                    while ((n = in.read(buf)) > 0) {
                        out.write(buf, 0, n);
                        out.flush();
                    }
                } catch (Exception ignored) {
                } finally {
                    try { out.close(); } catch (Exception ignored) {}
                    try { in.close(); } catch (Exception ignored) {}
                }
            }
        });
        t.setDaemon(true);
        t.start();
    }
}
