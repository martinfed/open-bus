package il.org.hasadna.siri_client.gtfs.crud;

import java.io.BufferedOutputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.nio.file.Files;
import java.nio.file.Path;

import org.apache.commons.net.ftp.FTP;
import org.apache.commons.net.ftp.FTPClient;
import org.apache.commons.net.ftp.FTPReply;

public class GtfsFtp {

	private static final String HOST = "gtfs.mot.gov.il";
	private static final String FILE_NAME = "israel-public-transportation.zip";

	FTPClient connect(String host) throws IOException {
		FTPClient ftpClient = createFTPClient();
		ftpClient.connect(host);
		ftpClient.login("anonymous", "");
		ftpClient.enterLocalPassiveMode();
		ftpClient.setFileType(FTP.BINARY_FILE_TYPE);
		if (!FTPReply.isPositiveCompletion(ftpClient.getReplyCode())) {
			throw new IOException("Faild to connect to: " + host);
		}
		return ftpClient;
	}

	public Path downloadGtfsZipFile() throws IOException {
		return downloadGtfsZipFile(createTempFile());
	}

	Path createTempFile() throws IOException {

		return Files.createTempFile(null, null);
	}

	private Path downloadGtfsZipFile(Path path) throws IOException {
		FTPClient conn = connect(HOST);
		OutputStream out = new BufferedOutputStream(new FileOutputStream(path.toFile()));

		if (!conn.retrieveFile(FILE_NAME, out)) {
			throw new IOException("Failed to download the file: " + FILE_NAME);
		}
		out.close();
		return path;
	}

	FTPClient createFTPClient() {
		return new FTPClient();
	}
}
