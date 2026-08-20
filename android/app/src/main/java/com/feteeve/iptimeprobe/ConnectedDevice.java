package com.feteeve.iptimeprobe;

final class ConnectedDevice {
    final String mac;
    final String ip;
    final String hostname;
    final String connection;
    final Integer rssi;

    ConnectedDevice(String mac, String ip, String hostname, String connection, Integer rssi) {
        this.mac = mac;
        this.ip = ip;
        this.hostname = hostname;
        this.connection = connection;
        this.rssi = rssi;
    }
}
