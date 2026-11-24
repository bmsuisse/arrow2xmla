import java.sql.Connection;
import java.sql.DriverManager;
import java.util.Arrays;
import org.olap4j.Cell;
import org.olap4j.CellSet;
import org.olap4j.OlapConnection;
import org.olap4j.Position;

/** Reads an answer through olap4j's XMLA driver (an independent client) and prints what it saw, one fact per line. */
public class Olap4jCheck {
  public static void main(String[] a) throws Exception {
    Class.forName("org.olap4j.driver.xmla.XmlaOlap4jDriver");
    try (Connection c = DriverManager.getConnection("jdbc:xmla:Server=" + a[0] + ";Catalog=Demo")) {
      OlapConnection oc = c.unwrap(OlapConnection.class);
      CellSet cs = oc.createStatement().executeOlapQuery(a[1]);
      System.out.println("AXES " + cs.getAxes().size());
      int cols = cs.getAxes().get(0).getPositionCount();
      int rows = cs.getAxes().get(1).getPositionCount();
      for (Position p : cs.getAxes().get(0).getPositions()) System.out.println("COLUMN " + p.getMembers().get(0).getCaption());
      for (Position p : cs.getAxes().get(1).getPositions()) System.out.println("ROW " + p.getMembers().get(0).getCaption());
      for (int r = 0; r < rows; r++) {
        for (int k = 0; k < cols; k++) {
          Cell cell = cs.getCell(Arrays.asList(k, r));
          Object v = cell.getValue();
          System.out.println("CELL " + r + " " + k + " " + (v == null ? "null" : v.getClass().getSimpleName() + " " + v));
        }
      }
    }
  }
}
