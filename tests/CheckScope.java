import java.io.*;
import java.util.*;
import java.util.zip.*;
import com.cypress.ezpdanalyzer.ui.model.GraphData;
class CheckScope {
 public static void main(String[] args) throws Exception {
  try (ZipFile z = new ZipFile(args[0])) {
   for (ZipEntry e : Collections.list(z.entries())) if(e.getName().endsWith(".scope")) {
    ArrayList<?> list=(ArrayList<?>)new ObjectInputStream(z.getInputStream(e)).readObject();
    GraphData g=(GraphData)list.get(0);
    if(list.size()!=1 || g.getAmp()!=-1 || g.getCc1()!=123 || g.getCc2()!=456 || g.getVolt()!=4095 || g.getTimeStamp()!=4294967306L) throw new AssertionError();
    System.out.println("Native GraphData deserialization OK");
   }
  }
 }
}
