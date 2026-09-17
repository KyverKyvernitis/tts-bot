package dev.core.worker;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Arrays;

public final class ProcessProbe {
    public static void main(String[] args) throws Exception {
        String mode = args[0];
        if (mode.equals("flood")) {
            byte[] block = new byte[8192]; Arrays.fill(block, (byte)'x');
            Thread error = new Thread(() -> { for (int i=0;i<128;i++) System.err.write(block,0,block.length); });
            error.start();
            for (int i=0;i<128;i++) System.out.write(block);
            error.join();
        } else if (mode.equals("bytes")) {
            System.out.print(args[1]);
        } else if (mode.equals("descendant")) {
            new ProcessBuilder(ProcessHarness.command("hold", args[1])).inheritIO().start();
        } else {
            Files.writeString(Path.of(args[1]), Long.toString(ProcessHandle.current().pid()));
            if (mode.equals("hold")) { Thread.sleep(10000); return; }
            byte[] block = new byte[8192];
            while (true) { System.out.write(block); System.err.write(block); }
        }
    }
}
