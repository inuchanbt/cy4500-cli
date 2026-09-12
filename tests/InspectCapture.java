import java.io.*;
import java.util.*;
import java.util.zip.*;
import com.cypress.ezpdanalyzer.ui.model.USBPacketData;
class InspectCapture {
 public static void main(String[] args) throws Exception {
  try (ZipFile z = new ZipFile(args[0]); OutputStream raw = new FileOutputStream(args[1])) {
   for (ZipEntry e : Collections.list(z.entries())) if(e.getName().endsWith(".part")) {
    ArrayList<?> list=(ArrayList<?>)new ObjectInputStream(z.getInputStream(e)).readObject();
    for(Object o:list) {USBPacketData p=(USBPacketData)o; raw.write(Arrays.copyOf(p.getPktData(),64)); System.out.println(p.getSno()+" "+p.getOk()+" len="+p.getPktData().length+" details="+p.getPacketDetails().size()+" payloads="+p.getPayloads().size()+" sub="+p.getSubPackets().size());}
   }
  }
 }
}

